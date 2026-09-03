# app/container.py —— 依赖容器（装配根，依赖倒置的汇聚点）
# 业务：集中创建适配器/工具/业务模块/总控图；FastAPI 启动与测试共用同一装配，便于替换 Mock（docs/01 §6）
import atexit
import sys
from pathlib import Path

from langgraph.checkpoint.memory import MemorySaver

from app.adapters.llm import LLMClient
from app.adapters.mock_oa import (
    MockEmailProvider,
    MockNotifyProvider,
    MockUserProvider,
    MockVerifyProvider,
)
from app.adapters.object_storage import build_object_storage
from app.adapters.pg_storage import PgStorage
from app.adapters.storage import SqliteStorage
from app.core import ids
from app.core.config import (
    AMOUNT_DIFF_THRESHOLD,
    ASYNC_ENABLED,
    CLAUSES_DIR,
    DB_PATH,
    LLM_API_KEY,
    POLICY_DIR,
    RAG_ENABLED,
    RAG_TOP_K,
    RAG_VECTOR_DSN,
    STORAGE_DSN,
    ensure_dirs,
)
from app.core.uploader import Uploader
from app.graphs.router_graph import build_router_graph
from app.registry import MODULE_BUILDERS
from app.shared.advance.service import AdvanceService
from app.shared.policies.loader import PolicyLoader
from app.shared.policies.rag import PolicyIndex
from app.shared.policies.vector_store import PolicyVectorStore
from app.tools.advance_tool import AdvanceTool
from app.tools.email_tool import EmailTool
from app.tools.llm_tool import LlmTool
from app.tools.notify_tool import NotifyTool
from app.tools.ocr_tool import OcrTool, PaddleOcrProvider
from app.tools.policy_rag_tool import PolicyRagTool
from app.tools.verify_tool import VerifyTool


class Container:
    """依赖容器：所有单例 + 工具 + 业务模块 + 总控图"""

    def __init__(
        self,
        *,
        storage,
        users,
        verify_provider,
        notify_provider,
        email_provider,
        policies,
        advance_service,
        amount_threshold,
        object_dir=None,
        checkpointer=None,
    ):
        # 适配器（外部系统边界）
        self.storage = storage
        self.users = users
        self.verify_provider = verify_provider
        self.notify_provider = notify_provider
        self.email_provider = email_provider
        # 共享服务
        self.policies = policies
        self.advances = advance_service
        self.amount_threshold = amount_threshold

        # 工具层（封装 provider，供图节点调用）
        # 业务：LLM 无 key 时 llm_tool 为 None，全链路降级确定性路径（同 vector_store 范式）；
        #       识别工具按票据形态分派（XML 直解析 / 文本模板库 / LLM 兜底，docs/04 §3）
        self.llm_tool = LlmTool(LLMClient()) if LLM_API_KEY else None
        # 业务：PaddleOcrProvider 惰性加载——组件未装时图片/扫描识别明确降级报错，
        #       XML/文本等核心格式不受影响（docs/04 §3.1 降级故事）
        self.ocr = OcrTool(llm_tool=self.llm_tool, image_ocr=PaddleOcrProvider())
        self.verify_tool = VerifyTool(verify_provider)
        self.advance_tool = AdvanceTool(advance_service)
        self.notify_tool = NotifyTool(notify_provider)
        self.email_tool = EmailTool(email_provider)

        # 制度条款 RAG（非结构化政策文本 → 按场景检索依据）
        # 业务：默认 BM25 词法检索离线可跑；设置 FLOWINVOICE_RAG_VECTOR_DSN（缺省回退主库 DSN）后启用
        #       "BM25 + bge-m3 向量"混合检索，PG 不可用自动降级 BM25（docs/03 §3）
        vector_store = PolicyVectorStore(RAG_VECTOR_DSN) if RAG_VECTOR_DSN else None
        self.policy_rag = PolicyRagTool(
            PolicyIndex(CLAUSES_DIR, top_k=RAG_TOP_K, vector_store=vector_store),
            enabled=RAG_ENABLED,
        )

        # 跨业务复用：对象存储 + 上传解析器（源文件进对象存储，DB 只存 file_key，docs/06 Phase 2）
        # 业务：测试传 object_dir 隔离到 tmp；生产缺省全局 OBJECT_DIR / MinIO（配置端点时）
        self.object_storage = build_object_storage(local_root=object_dir)
        self.uploader = Uploader(self.object_storage)

        # 业务模块（注册表装配：新增业务只改 registry.py）
        self.businesses = {name: builder(self) for name, builder in MODULE_BUILDERS.items()}

        # HITL checkpointer：同步 MemorySaver（默认）/ 异步持久化（PG 或 SQLite，见 build_checkpointer）
        # 作用：编译图时注入，interrupt() 挂起点的 checkpoint 写入持久化库 → decide 恢复跨进程可用
        self.checkpointer = checkpointer if checkpointer is not None else MemorySaver()

        # 总控图（依赖上面全部，构建一次复用）
        self.graph = build_router_graph(self)


def _force_utf8_console() -> None:
    """把标准输出/错误流切到 UTF-8（中文 Windows 控制台默认 GBK，打印 ¥ 等会报错）"""
    # 作用：Mock 通知/邮件会 print 中文与货币符号，统一编码避免 UnicodeEncodeError
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass  # 某些环境（pytest 重定向）不支持 reconfigure，忽略


def build_container(db_path=None) -> Container:
    """装配依赖容器（FastAPI 启动与测试共用）"""
    # 作用：确保数据目录存在后再建库
    _force_utf8_console()
    ensure_dirs()
    # 作用：生产走 PostgreSQL（配置 FLOWINVOICE_PG_DSN），否则 SQLite 测试替身（docs/06 §4）
    # 业务：PG 不可用且已配置 DSN → 启动即失败（存储是核心依赖，不静默降级到 SQLite）
    storage = PgStorage(STORAGE_DSN) if STORAGE_DSN else SqliteStorage(db_path or DB_PATH)
    # 作用：测试传 db_path（tmp）时对象存储同样隔离到同目录下 objects/，不污染全局 data/objects
    object_dir = Path(db_path).parent / "objects" if db_path else None
    # 作用：重启后序号续接已有单号，避免复用旧号（messages/emails 只追加，同号复用会让新单混入历史留痕）
    ids.seed_from_existing(
        [r["request_id"] for r in storage.list_requests()]
        + [a["app_id"] for a in storage.list_advances()]
    )
    policies = PolicyLoader(POLICY_DIR)
    return Container(
        storage=storage,
        users=MockUserProvider(),
        verify_provider=MockVerifyProvider(),
        notify_provider=MockNotifyProvider(storage),
        email_provider=MockEmailProvider(storage),
        policies=policies,
        advance_service=AdvanceService(storage, policies),
        amount_threshold=AMOUNT_DIFF_THRESHOLD,
        object_dir=object_dir,
        checkpointer=build_checkpointer(storage, db_path),
    )


def build_checkpointer(storage, db_path=None):
    """HITL checkpointer 三态选择（#52 D1）——同步/异步共用同一装配入口

    | 模式       | 条件                    | checkpointer                      |
    |-----------|-------------------------|-----------------------------------|
    | 同步（默认）| FLOWINVOICE_ASYNC 未开   | MemorySaver（进程内，历史行为不变）|
    | 异步 + PG  | ASYNC=1 且 PgStorage     | PostgresSaver（跨进程共享）       |
    | 异步 + 本地| ASYNC=1 且 SqliteStorage | SqliteSaver（db 同目录替身）      |

    作用：异步模式下 API 与 worker 各自 build_container()，checkpointer 指向同一持久化库
          → worker 把挂起 checkpoint 写入库，API 进程 decide 经 Command(resume=...) 跨进程恢复。
    注意：不能用 from_conn_string()（内部 with closing()，块退出即关连接）——长驻进程必须
          自持连接/连接池再传构造函数，saver 与容器同生命周期（docs/11 F4 实测结论）。
    """
    if not ASYNC_ENABLED:
        return MemorySaver()
    if isinstance(storage, PgStorage):
        # 生产：独立 psycopg_pool 连接池（多线程安全，与 PgStorage 各持各池互不干扰）。
        # PostgresSaver 不自管事务（get_connection 借池连接归还即 rollback）→ 连接必须 autocommit，
        # 且 setup() 里 CREATE INDEX CONCURRENTLY 不能在事务内跑；dict_row/prepare_threshold 与
        # 官方 from_conn_string 一致（docs/11 F5 实测结论）
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool
        from langgraph.checkpoint.postgres import PostgresSaver

        pool = ConnectionPool(
            STORAGE_DSN,
            min_size=1,
            max_size=5,
            open=True,
            kwargs={"autocommit": True, "prepare_threshold": 0, "row_factory": dict_row},
        )
        atexit.register(pool.close)  # 进程退出回收连接
        cp = PostgresSaver(pool)
    else:
        # 本地异步替身：checkpoint 文件与业务库同目录（测试传 tmp db_path → 隔离到 tmp）
        import sqlite3

        from langgraph.checkpoint.sqlite import SqliteSaver

        ckpt_path = Path(db_path).parent / "checkpoints.sqlite" if db_path else DB_PATH.parent / "checkpoints.sqlite"
        conn = sqlite3.connect(str(ckpt_path), check_same_thread=False)
        cp = SqliteSaver(conn)
    cp.setup()  # 建 checkpoints 表（幂等）
    return cp
