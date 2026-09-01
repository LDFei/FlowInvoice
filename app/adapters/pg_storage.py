# app/adapters/pg_storage.py —— PostgreSQL 生产存储（StorageProvider 实现，docs/06）
# 业务：生产主库；requests 整状态 JSONB + invoices/approval_records 拆表 + 发票池唯一约束查重
#       SqliteStorage 仅作测试/离线替身；未配置 FLOWINVOICE_PG_DSN 时容器自动走 SqliteStorage
# 线程安全：psycopg_pool 连接池按需借用连接，跨线程不共享同一连接（FastAPI 线程 + Celery worker 共用）
from datetime import datetime
from decimal import Decimal

from app.adapters.base import StorageProvider
from app.core.json_codec import dumps as _json_dumps

# 作用：psycopg / psycopg_pool 惰性导入——纯 SQLite 离线运行不强制依赖 PG 栈（同 vector_store 范式）

_DDL = """
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    state_json   JSONB NOT NULL,
    status       TEXT NOT NULL,
    current_step TEXT NOT NULL,
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_requests_status ON requests (status, created_at DESC);

-- 注意：submissions 无 FK 到 requests——任务行先于请求行创建（提交即落，成功才写 requests），跨事务
CREATE TABLE IF NOT EXISTS submissions (
    request_id TEXT PRIMARY KEY,
    snapshot   JSONB NOT NULL,
    status     TEXT NOT NULL,
    error      JSONB,
    attempts   INT NOT NULL DEFAULT 0,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_submissions_status ON submissions (status, created_at);

CREATE TABLE IF NOT EXISTS invoices (
    id           BIGSERIAL PRIMARY KEY,
    invoice_no   TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    invoice_type TEXT,
    date         TEXT,
    amount       NUMERIC(14,2),
    title        TEXT,
    file_key     TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_active_no ON invoices (invoice_no) WHERE status = 'active';
CREATE INDEX IF NOT EXISTS idx_invoices_request ON invoices (request_id);

CREATE TABLE IF NOT EXISTS approval_records (
    id         BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL REFERENCES requests(request_id),
    role       TEXT NOT NULL,
    decision   TEXT NOT NULL,
    actor      TEXT,
    comment    TEXT,
    time       TIMESTAMPTZ NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_approval_records_request ON approval_records (request_id);

CREATE TABLE IF NOT EXISTS advance_applications (
    app_id           TEXT PRIMARY KEY,
    employee_id      TEXT NOT NULL,
    direction        TEXT NOT NULL,
    start_date       TEXT NOT NULL,
    end_date         TEXT NOT NULL,
    valid_until      TEXT NOT NULL,
    estimated_amount NUMERIC(14,2) NOT NULL,
    purpose          TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_advance_direction ON advance_applications (employee_id, direction, status);

CREATE TABLE IF NOT EXISTS messages (
    id         BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL,
    to_role    TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_messages_request ON messages (request_id);

CREATE TABLE IF NOT EXISTS emails (
    id         BIGSERIAL PRIMARY KEY,
    request_id TEXT NOT NULL,
    to_address TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_emails_request ON emails (request_id);
"""


def _now() -> str:
    # 作用：统一时间戳格式（ISO，可排序；PG TIMESTAMPTZ 自动解析）
    return datetime.now().isoformat(timespec="seconds")


def _to_float(v):
    # 作用：PG NUMERIC 列读回是 Decimal；对外统一 float（与 SQLite REAL 行为一致，业务层无感）
    # 业务：真正的 Decimal 金额数学（docs/06 §3.4 / #44）在解析层做，存储层保持行为等价
    return float(v) if isinstance(v, Decimal) else v


class PgStorage(StorageProvider):
    """PostgreSQL 持久化（连接池；SQL 用 psycopg3 参数占位 %s）"""

    def __init__(self, dsn: str):
        self._dsn = dsn
        self._pool = None
        # 作用：启动即建表（幂等）——存储是核心依赖，PG 不可用应当启动即失败而非运行期才暴露
        self.ensure_schema()
        # 作用：进程退出时显式关池，避免 psycopg_pool 后台线程在 __del__ 时未停导致的日志噪音
        import atexit

        atexit.register(self.close)

    def close(self) -> None:
        """关闭连接池（FastAPI lifespan 关闭 / 测试 teardown / 进程退出时调用）"""
        if self._pool is not None:
            self._pool.close()
            self._pool = None

    # ================= 连接池 =================

    def _get_pool(self):
        # 作用：惰性创建连接池（首次使用才连；建连超时 3s、单查询超时 10s 防卡死）
        if self._pool is None:
            from psycopg_pool import ConnectionPool

            self._pool = ConnectionPool(
                self._dsn,
                min_size=1,
                max_size=10,
                open=False,
                kwargs={"connect_timeout": 3, "options": "-c statement_timeout=10000"},
                configure=self._configure_conn,
            )
            self._pool.open()
        return self._pool

    @staticmethod
    def _configure_conn(conn) -> None:
        # 作用：新连接统一返回 dict 行
        from psycopg.rows import dict_row

        conn.row_factory = dict_row

    def ensure_schema(self) -> None:
        """建表 + 索引（幂等）；启动时调用"""
        from psycopg.types.json import Jsonb  # noqa: F401 —— 确保 JSONB 类型注册

        with self._get_pool().connection() as conn:
            conn.execute(_DDL)
            conn.commit()

    def _conn(self):
        # 作用：借用连接（with 内用完归还池）
        return self._get_pool().connection()

    @staticmethod
    def _jsonb(obj):
        from psycopg.types.json import Jsonb

        # 作用：state 允许含 Decimal（PG NUMERIC 读回类型），序列化兜底转字符串（docs/06 §3.4）
        return Jsonb(obj, dumps=_json_dumps)

    # ================= 报销请求 =================

    def upsert_request(self, request_id: str, state: dict, status: str, current_step: str) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO requests (request_id, state_json, status, current_step, created_at, updated_at)
                   VALUES (%s, %s, %s, %s, %s, %s)
                   ON CONFLICT (request_id) DO UPDATE SET
                     state_json = EXCLUDED.state_json,
                     status = EXCLUDED.status,
                     current_step = EXCLUDED.current_step,
                     updated_at = EXCLUDED.updated_at""",
                (request_id, self._jsonb(state), status, current_step, _now(), _now()),
            )
            conn.commit()

    def get_request(self, request_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT state_json FROM requests WHERE request_id = %s", (request_id,),
            ).fetchone()
            return row["state_json"] if row else None  # JSONB 自动反序列化为 dict

    def list_requests(self, status: str | None = None) -> list[dict]:
        with self._conn() as conn:
            sql = "SELECT * FROM requests"
            params: tuple = ()
            if status:
                sql += " WHERE status = %s"
                params = (status,)
            sql += " ORDER BY created_at DESC"
            rows = conn.execute(sql, params).fetchall()
        summaries = []
        for row in rows:
            state = row["state_json"]
            invoice = state.get("invoice_data") or {}
            summaries.append({
                "request_id": row["request_id"],
                "status": row["status"],
                "current_step": row["current_step"],
                "business_type": state.get("business_type", ""),
                "amount": invoice.get("amount"),
                "employee_id": state.get("invoice_input", {}).get("employee_id", ""),
                "created_at": row["created_at"].isoformat(),
                "updated_at": row["updated_at"].isoformat(),
            })
        return summaries

    # ================= 事前申请 =================

    def create_advance(self, advance: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO advance_applications
                     (app_id, employee_id, direction, start_date, end_date, valid_until,
                      estimated_amount, purpose, status, created_at)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                   ON CONFLICT (app_id) DO UPDATE SET status = EXCLUDED.status""",
                (
                    advance["app_id"], advance["employee_id"], advance["direction"],
                    advance["start_date"], advance["end_date"], advance["valid_until"],
                    advance["estimated_amount"], advance["purpose"], advance["status"],
                    advance.get("created_at", _now()),
                ),
            )
            conn.commit()

    def get_advance(self, app_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM advance_applications WHERE app_id = %s", (app_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["estimated_amount"] = _to_float(d.get("estimated_amount"))
            d["created_at"] = d["created_at"].isoformat()
            return d

    def find_active_advance(self, employee_id: str, direction: str, on_date: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                """SELECT * FROM advance_applications
                   WHERE employee_id = %s AND direction = %s AND status = 'active'
                     AND start_date <= %s AND end_date >= %s AND valid_until >= %s
                   ORDER BY created_at ASC LIMIT 1""",
                (employee_id, direction, on_date, on_date, on_date),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["estimated_amount"] = _to_float(d.get("estimated_amount"))
            d["created_at"] = d["created_at"].isoformat()
            return d

    def list_advances(self, status: str | None = None) -> list[dict]:
        with self._conn() as conn:
            sql = "SELECT * FROM advance_applications"
            params: tuple = ()
            if status:
                sql += " WHERE status = %s"
                params = (status,)
            sql += " ORDER BY created_at DESC"
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["estimated_amount"] = _to_float(d.get("estimated_amount"))
            d["created_at"] = d["created_at"].isoformat()
            out.append(d)
        return out

    # ================= 通知 / 邮件留痕 =================

    def add_message(self, request_id: str, to_role: str, content: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO messages (request_id, to_role, content, created_at) VALUES (%s, %s, %s, %s)",
                (request_id, to_role, content, _now()),
            )
            conn.commit()

    def list_messages(self, request_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM messages WHERE request_id = %s ORDER BY id", (request_id,),
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["created_at"] = d["created_at"].isoformat()
            out.append(d)
        return out

    def add_email(self, request_id: str, to: str, subject: str, body: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO emails (request_id, to_address, subject, body, created_at) VALUES (%s, %s, %s, %s, %s)",
                (request_id, to, subject, body, _now()),
            )
            conn.commit()

    def list_emails(self, request_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM emails WHERE request_id = %s ORDER BY id", (request_id,),
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["created_at"] = d["created_at"].isoformat()
            out.append(d)
        return out

    # ================= 异步任务（submissions） =================

    def create_submission(self, request_id: str, snapshot: dict, status: str = "pending") -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO submissions (request_id, snapshot, status, error, attempts, created_at, updated_at)
                   VALUES (%s, %s, %s, NULL, 0, %s, %s)""",
                (request_id, self._jsonb(snapshot), status, _now(), _now()),
            )
            conn.commit()

    def get_submission(self, request_id: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM submissions WHERE request_id = %s", (request_id,)).fetchone()
            if not row:
                return None
            d = dict(row)
            d["created_at"] = d["created_at"].isoformat()
            d["updated_at"] = d["updated_at"].isoformat()
            return d  # snapshot/error 为 JSONB，已自动解析为 dict

    def update_submission(
        self,
        request_id: str,
        *,
        status: str | None = None,
        error: dict | None = None,
        attempts: int | None = None,
    ) -> None:
        # 作用：只更新传入字段（动态 SET）
        fields, params = [], []
        if status is not None:
            fields.append("status = %s"); params.append(status)
        if error is not None:
            fields.append("error = %s"); params.append(self._jsonb(error))
        if attempts is not None:
            fields.append("attempts = %s"); params.append(attempts)
        if not fields:
            return
        params.append(_now())
        params.append(request_id)
        with self._conn() as conn:
            conn.execute(
                f"UPDATE submissions SET {', '.join(fields)}, updated_at = %s WHERE request_id = %s",
                tuple(params),
            )
            conn.commit()

    def list_submissions(self, status: str | None = None) -> list[dict]:
        with self._conn() as conn:
            sql = "SELECT * FROM submissions"
            params: tuple = ()
            if status:
                sql += " WHERE status = %s"
                params = (status,)
            sql += " ORDER BY created_at DESC"
            rows = conn.execute(sql, params).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["created_at"] = d["created_at"].isoformat()
            d["updated_at"] = d["updated_at"].isoformat()
            out.append(d)
        return out

    def reset_stuck_submissions(self) -> int:
        with self._conn() as conn:
            cur = conn.execute(
                "UPDATE submissions SET status = 'pending', updated_at = %s WHERE status = 'processing'",
                (_now(),),
            )
            conn.commit()
            return cur.rowcount

    # ================= 发票池（真查重） =================

    def add_invoice(self, invoice: dict) -> bool:
        # 作用：active 票号部分唯一索引拦截重复 → UniqueViolation → False
        from psycopg.errors import UniqueViolation

        try:
            with self._conn() as conn:
                conn.execute(
                    """INSERT INTO invoices (invoice_no, request_id, invoice_type, date, amount, title, file_key, status, created_at)
                       VALUES (%s, %s, %s, %s, %s, %s, %s, 'active', %s)""",
                    (
                        invoice["invoice_no"], invoice.get("request_id", ""),
                        invoice.get("invoice_type", ""), invoice.get("date", ""),
                        str(invoice.get("amount", "")), invoice.get("title", ""),
                        invoice.get("file_key", ""), _now(),
                    ),
                )
                conn.commit()
                return True
        except UniqueViolation:
            return False

    def release_invoice(self, request_id: str) -> None:
        with self._conn() as conn:
            conn.execute(
                "UPDATE invoices SET status = 'released' WHERE request_id = %s AND status = 'active'",
                (request_id,),
            )
            conn.commit()

    def find_invoice(self, invoice_no: str) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT * FROM invoices WHERE invoice_no = %s AND status = 'active' LIMIT 1",
                (invoice_no,),
            ).fetchone()
            if not row:
                return None
            d = dict(row)
            d["amount"] = _to_float(d.get("amount"))
            return d

    # ================= 审批记录（审计） =================

    def add_approval_record(self, request_id: str, record: dict) -> None:
        with self._conn() as conn:
            conn.execute(
                """INSERT INTO approval_records (request_id, role, decision, actor, comment, time)
                   VALUES (%s, %s, %s, %s, %s, %s)""",
                (
                    request_id,
                    record.get("role", ""),
                    record.get("decision", ""),
                    record.get("actor", ""),
                    record.get("comment", ""),
                    record.get("time", _now()),
                ),
            )
            conn.commit()

    def list_approval_records(self, request_id: str) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_records WHERE request_id = %s ORDER BY id", (request_id,),
            ).fetchall()
        out = []
        for row in rows:
            d = dict(row)
            d["time"] = d["time"].isoformat()
            out.append(d)
        return out
