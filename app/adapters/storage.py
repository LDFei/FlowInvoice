# app/adapters/storage.py —— SQLite 持久化（StorageProvider 实现）
# 业务：本地 Demo 落库，无需外部服务即可运行；换 PostgreSQL 时只实现同接口即可
import json
import sqlite3
import threading
from datetime import datetime

from app.adapters.base import StorageProvider
from app.core.json_codec import dumps as _json_dumps

# 作用：建表语句（幂等，CREATE IF NOT EXISTS）
# 业务：请求整状态 JSON 存储，保证 LangGraph 中断恢复/状态查询都拿得到完整数据
_SCHEMA = """
CREATE TABLE IF NOT EXISTS requests (
    request_id   TEXT PRIMARY KEY,
    state_json   TEXT NOT NULL,
    status       TEXT NOT NULL,
    current_step TEXT NOT NULL,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS advance_applications (
    app_id           TEXT PRIMARY KEY,
    employee_id      TEXT NOT NULL,
    direction        TEXT NOT NULL,
    start_date       TEXT NOT NULL,
    end_date         TEXT NOT NULL,
    valid_until      TEXT NOT NULL,
    estimated_amount REAL NOT NULL,
    purpose          TEXT NOT NULL,
    status           TEXT NOT NULL,
    created_at       TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    to_role    TEXT NOT NULL,
    content    TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS emails (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    to_address TEXT NOT NULL,
    subject    TEXT NOT NULL,
    body       TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS submissions (
    request_id TEXT PRIMARY KEY,
    snapshot   TEXT NOT NULL,
    status     TEXT NOT NULL,
    error      TEXT,
    attempts   INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_no   TEXT NOT NULL,
    request_id   TEXT NOT NULL,
    invoice_type TEXT,
    date         TEXT,
    amount       TEXT,
    title        TEXT,
    file_key     TEXT,
    status       TEXT NOT NULL DEFAULT 'active',
    created_at   TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_invoices_active_no
    ON invoices (invoice_no) WHERE status = 'active';
CREATE TABLE IF NOT EXISTS approval_records (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    request_id TEXT NOT NULL,
    role       TEXT NOT NULL,
    decision   TEXT NOT NULL,
    actor      TEXT,
    comment    TEXT,
    time       TEXT NOT NULL
);
"""


def _now() -> str:
    # 作用：统一时间戳格式（ISO，可排序）
    return datetime.now().isoformat(timespec="seconds")


class SqliteStorage(StorageProvider):
    """基于 SQLite 的持久化实现（线程安全：全局锁 + 每次操作独立连接）"""

    def __init__(self, db_path):
        # 作用：初始化表结构
        # 业务：应用启动时调用一次即可
        self._db_path = str(db_path)
        self._lock = threading.Lock()
        with self._lock:
            conn = sqlite3.connect(self._db_path)
            try:
                conn.executescript(_SCHEMA)
            finally:
                conn.close()

    def _conn(self) -> sqlite3.Connection:
        # 作用：每次操作新建连接，避免跨线程共享连接导致锁库
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    # ================= 报销请求 =================

    def upsert_request(self, request_id: str, state: dict, status: str, current_step: str) -> None:
        # 作用：INSERT ... ON CONFLICT 整体覆盖保存
        # 业务：每次图执行完（中断点/终态）都落一次，重启后状态仍在
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO requests (request_id, state_json, status, current_step, created_at, updated_at)
                       VALUES (?, ?, ?, ?, ?, ?)
                       ON CONFLICT(request_id) DO UPDATE SET
                         state_json = excluded.state_json,
                         status = excluded.status,
                         current_step = excluded.current_step,
                         updated_at = excluded.updated_at""",
                    (request_id, _json_dumps(state), status, current_step, _now(), _now()),
                )
                conn.commit()
            finally:
                conn.close()

    def get_request(self, request_id: str) -> dict | None:
        # 作用：按单号取回完整状态
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT state_json FROM requests WHERE request_id = ?", (request_id,)).fetchone()
                return json.loads(row["state_json"]) if row else None
            finally:
                conn.close()

    def list_requests(self, status: str | None = None) -> list[dict]:
        # 作用：返回请求摘要列表（管理端列表/驾驶舱数据源）
        with self._lock:
            conn = self._conn()
            try:
                sql = "SELECT * FROM requests"
                params: tuple = ()
                if status:
                    sql += " WHERE status = ?"
                    params = (status,)
                sql += " ORDER BY created_at DESC"
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        summaries = []
        for row in rows:
            state = json.loads(row["state_json"])
            invoice = state.get("invoice_data") or {}
            summaries.append({
                "request_id": row["request_id"],
                "status": row["status"],
                "current_step": row["current_step"],
                "business_type": state.get("business_type", ""),
                "amount": invoice.get("amount"),
                "employee_id": state.get("invoice_input", {}).get("employee_id", ""),
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
            })
        return summaries

    # ================= 事前申请 =================

    def create_advance(self, advance: dict) -> None:
        # 作用：upsert 事前申请（含状态流转 used/expired）
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO advance_applications
                         (app_id, employee_id, direction, start_date, end_date, valid_until,
                          estimated_amount, purpose, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                       ON CONFLICT(app_id) DO UPDATE SET status = excluded.status""",
                    (
                        advance["app_id"], advance["employee_id"], advance["direction"],
                        advance["start_date"], advance["end_date"], advance["valid_until"],
                        advance["estimated_amount"], advance["purpose"], advance["status"],
                        advance.get("created_at", _now()),
                    ),
                )
                conn.commit()
            finally:
                conn.close()

    def get_advance(self, app_id: str) -> dict | None:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM advance_applications WHERE app_id = ?", (app_id,)).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def find_active_advance(self, employee_id: str, direction: str, on_date: str) -> dict | None:
        # 业务：差旅报销时匹配 —— 员工+方向 + 申请区间覆盖报销日期 + 未过有效期 + 状态 active
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    """SELECT * FROM advance_applications
                       WHERE employee_id = ? AND direction = ? AND status = 'active'
                         AND start_date <= ? AND end_date >= ? AND valid_until >= ?
                       ORDER BY created_at ASC LIMIT 1""",
                    (employee_id, direction, on_date, on_date, on_date),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    def list_advances(self, status: str | None = None) -> list[dict]:
        with self._lock:
            conn = self._conn()
            try:
                sql = "SELECT * FROM advance_applications"
                params: tuple = ()
                if status:
                    sql += " WHERE status = ?"
                    params = (status,)
                sql += " ORDER BY created_at DESC"
                return [dict(r) for r in conn.execute(sql, params).fetchall()]
            finally:
                conn.close()

    # ================= 通知 / 邮件留痕 =================

    def add_message(self, request_id: str, to_role: str, content: str) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO messages (request_id, to_role, content, created_at) VALUES (?, ?, ?, ?)",
                    (request_id, to_role, content, _now()),
                )
                conn.commit()
            finally:
                conn.close()

    def list_messages(self, request_id: str) -> list[dict]:
        with self._lock:
            conn = self._conn()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM messages WHERE request_id = ? ORDER BY id", (request_id,),
                ).fetchall()]
            finally:
                conn.close()

    def add_email(self, request_id: str, to: str, subject: str, body: str) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "INSERT INTO emails (request_id, to_address, subject, body, created_at) VALUES (?, ?, ?, ?, ?)",
                    (request_id, to, subject, body, _now()),
                )
                conn.commit()
            finally:
                conn.close()

    def list_emails(self, request_id: str) -> list[dict]:
        with self._lock:
            conn = self._conn()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM emails WHERE request_id = ? ORDER BY id", (request_id,),
                ).fetchall()]
            finally:
                conn.close()

    # ================= 异步任务（submissions） =================

    def create_submission(self, request_id: str, snapshot: dict, status: str = "pending") -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO submissions (request_id, snapshot, status, error, attempts, created_at, updated_at)
                       VALUES (?, ?, ?, NULL, 0, ?, ?)""",
                    (request_id, _json_dumps(snapshot), status, _now(), _now()),
                )
                conn.commit()
            finally:
                conn.close()

    def get_submission(self, request_id: str) -> dict | None:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute("SELECT * FROM submissions WHERE request_id = ?", (request_id,)).fetchone()
                if not row:
                    return None
                d = dict(row)
                d["snapshot"] = json.loads(d["snapshot"])
                d["error"] = json.loads(d["error"]) if d.get("error") else None
                return d
            finally:
                conn.close()

    def update_submission(
        self,
        request_id: str,
        *,
        status: str | None = None,
        error: dict | None = None,
        attempts: int | None = None,
    ) -> None:
        # 作用：只更新传入的字段（SQLite 无原生 COALESCE 简洁写法，用动态 SET）
        fields, params = [], []
        if status is not None:
            fields.append("status = ?"); params.append(status)
        if error is not None:
            fields.append("error = ?"); params.append(_json_dumps(error))
        if attempts is not None:
            fields.append("attempts = ?"); params.append(attempts)
        if not fields:
            return
        with self._lock:
            conn = self._conn()
            try:
                # 占位符顺序 = [各字段..., updated_at, request_id]；request_id 必须放最后，
                # 否则 WHERE 拿到时间戳 → 静默更新 0 行（docs/11 F1）
                conn.execute(
                    f"UPDATE submissions SET {', '.join(fields)}, updated_at = ? WHERE request_id = ?",
                    (*params, _now(), request_id),
                )
                conn.commit()
            finally:
                conn.close()

    def list_submissions(self, status: str | None = None) -> list[dict]:
        with self._lock:
            conn = self._conn()
            try:
                sql = "SELECT * FROM submissions"
                params: tuple = ()
                if status:
                    sql += " WHERE status = ?"
                    params = (status,)
                sql += " ORDER BY created_at DESC"
                rows = conn.execute(sql, params).fetchall()
            finally:
                conn.close()
        out = []
        for row in rows:
            d = dict(row)
            d["snapshot"] = json.loads(d["snapshot"])
            d["error"] = json.loads(d["error"]) if d.get("error") else None
            out.append(d)
        return out

    def reset_stuck_submissions(self) -> int:
        with self._lock:
            conn = self._conn()
            try:
                cur = conn.execute(
                    "UPDATE submissions SET status = 'pending', updated_at = ? WHERE status = 'processing'",
                    (_now(),),
                )
                conn.commit()
                return cur.rowcount
            finally:
                conn.close()

    # ================= 发票池（真查重） =================

    def add_invoice(self, invoice: dict) -> bool:
        # 作用：active 票号部分唯一索引拦截重复；冲突 → IntegrityError → False
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO invoices (invoice_no, request_id, invoice_type, date, amount, title, file_key, status, created_at)
                       VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?)""",
                    (
                        invoice["invoice_no"], invoice.get("request_id", ""),
                        invoice.get("invoice_type", ""), invoice.get("date", ""),
                        str(invoice.get("amount", "")), invoice.get("title", ""),
                        invoice.get("file_key", ""), _now(),
                    ),
                )
                conn.commit()
                return True
            except sqlite3.IntegrityError:
                return False
            finally:
                conn.close()

    def release_invoice(self, request_id: str) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    "UPDATE invoices SET status = 'released' WHERE request_id = ? AND status = 'active'",
                    (request_id,),
                )
                conn.commit()
            finally:
                conn.close()

    def find_invoice(self, invoice_no: str) -> dict | None:
        with self._lock:
            conn = self._conn()
            try:
                row = conn.execute(
                    "SELECT * FROM invoices WHERE invoice_no = ? AND status = 'active' LIMIT 1",
                    (invoice_no,),
                ).fetchone()
                return dict(row) if row else None
            finally:
                conn.close()

    # ================= 审批记录（审计） =================

    def add_approval_record(self, request_id: str, record: dict) -> None:
        with self._lock:
            conn = self._conn()
            try:
                conn.execute(
                    """INSERT INTO approval_records (request_id, role, decision, actor, comment, time)
                       VALUES (?, ?, ?, ?, ?, ?)""",
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
            finally:
                conn.close()

    def list_approval_records(self, request_id: str) -> list[dict]:
        with self._lock:
            conn = self._conn()
            try:
                return [dict(r) for r in conn.execute(
                    "SELECT * FROM approval_records WHERE request_id = ? ORDER BY id", (request_id,),
                ).fetchall()]
            finally:
                conn.close()
