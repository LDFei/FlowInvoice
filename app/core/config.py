# app/core/config.py —— 全局路径与基础配置
# 业务：统一项目根路径/数据目录/上传目录/策略目录，避免各模块各自拼接路径
import os
from pathlib import Path

from dotenv import load_dotenv

# 作用：项目根目录 = 本文件向上两级（app/core -> app -> 项目根）
# 业务：无论从哪个目录启动服务，路径都稳定指向项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 作用：加载项目根 .env（本地开发环境变量：数据库 DSN、LLM Key 等）
# 业务：有 .env 就注入环境变量；默认不覆盖已存在的系统变量（系统变量优先）
load_dotenv(PROJECT_ROOT / ".env")

# 作用：运行期数据目录（数据库 / 上传临时文件）
# 业务：本地 Demo 落盘于此；后续可替换为对象存储 / 独立数据库
DATA_DIR = PROJECT_ROOT / "data"

# 作用：上传文件临时目录
# 业务：Uploader 落盘后交给 OCR 工具读取
UPLOAD_DIR = DATA_DIR / "uploads"

# 作用：对象存储本地替身目录（无 MinIO 环境时 LocalObjectStorage 落盘于此）
# 业务：发票源文件持久副本（docs/06 Phase 2：文件不进 DB，只存 file_key）
OBJECT_DIR = DATA_DIR / "objects"

# 作用：SQLite 数据库文件
# 业务：请求/事前申请/消息/邮件统一持久化于此
DB_PATH = DATA_DIR / "flowinvoice.db"

# 作用：政策规则 YAML 目录
# 业务：一个业务一个 YAML（docs/AGENTS.md §9 配置驱动）
POLICY_DIR = PROJECT_ROOT / "app" / "policy"

# 作用：制度条款语料目录（非结构化政策文本，RAG 检索源）
# 业务：差旅/招待/票据/通用原则等条款，按场景检索作"依据"（docs/03 §3）
CLAUSES_DIR = POLICY_DIR / "clauses"

# 作用：是否启用制度条款 RAG 检索（env 可关：FLOWINVOICE_RAG_ENABLED=0/false）
# 业务：关闭时合规节点只做确定性检查
RAG_ENABLED = os.environ.get("FLOWINVOICE_RAG_ENABLED", "1").lower() in ("1", "true", "yes")

# 作用：每次检索返回的条款条数
RAG_TOP_K = 3

# 作用：主存储的 PostgreSQL DSN（生产主库：requests/submissions/invoices/审批记录，docs/06）
# 业务：设置后容器装配 PgStorage（生产），为空则 SqliteStorage（测试/离线替身）
STORAGE_DSN = os.environ.get("FLOWINVOICE_PG_DSN", "")

# 作用：政策条款向量检索的 PostgreSQL DSN（pgvector，独立可配）
# 业务：设置后启用"BM25 + bge-m3 向量"混合检索（docs/03 §3）；为空则只走 BM25。
#       默认回退到主库 DSN（一个 PG 即可两用）；单独设置即可"主库留 SQLite、只开向量 RAG"
#       ——与 STORAGE_DSN 解耦（docs/11 F2），避免一变量三用无法拆分
RAG_VECTOR_DSN = os.environ.get("FLOWINVOICE_RAG_VECTOR_DSN", "") or STORAGE_DSN

# 作用：LLM 接入配置（DeepSeek，OpenAI 兼容 Chat Completions 接口）
# 业务：无 key 时全链路自动降级确定性路径（规则/正则/模板），Demo 仍可离线运行
#       （LLM 只做"理解与表达"，不碰钱的判定，见 docs/03 §3 混合决策）
LLM_API_KEY = os.environ.get("FLOWINVOICE_LLM_API_KEY", "")
LLM_MODEL = os.environ.get("FLOWINVOICE_LLM_MODEL", "deepseek-v4-flash")
LLM_BASE_URL = os.environ.get("FLOWINVOICE_LLM_BASE_URL", "https://api.deepseek.com")

# 作用：金额误差容差（申报金额 vs 票面金额 差异比例）
# 业务：超过该比例 → 打风险标记，提示申报人填写有误
AMOUNT_DIFF_THRESHOLD = 0.05

# ===== 上传安全（app/core/uploader.py） =====
# 作用：上传文件大小上限（分块写入时超出即中止并拒绝，防超大文件打满磁盘）
# 业务：发票形态单文件一般 KB~几 MB，10MB 富余；生产可按需调（FLOWINVOICE_MAX_UPLOAD_BYTES）
MAX_UPLOAD_BYTES = int(os.environ.get("FLOWINVOICE_MAX_UPLOAD_BYTES", str(10 * 1024 * 1024)))

# 作用：允许上传的扩展名白名单（发票形态：图片 / PDF / 数电票 XML / Demo 文本票面）
# 业务：白名单外（exe/脚本/任意文件）直接拒绝——接收未知类型文件是上传入口最常见的安全面
ALLOWED_UPLOAD_EXTENSIONS = {".jpg", ".jpeg", ".png", ".bmp", ".webp", ".pdf", ".xml", ".ofd", ".txt"}

# 作用：上传临时文件保留天数（到期清理，源文件权威副本在对象存储，本地临时文件是处理缓存）
# 业务：防 data/uploads 无限增长；启动时 sweep（FLOWINVOICE_UPLOAD_RETENTION_DAYS 可调）
UPLOAD_RETENTION_DAYS = int(os.environ.get("FLOWINVOICE_UPLOAD_RETENTION_DAYS", "7"))

# 作用：演示员工号（Demo 不接登录，固定申报人）
# 业务：真实系统由登录态注入；此处简化便于跑通闭环
DEMO_EMPLOYEE_ID = "1001"

# ===== 对象存储 MinIO（app/adapters/object_storage.py，docs/06 Phase 2） =====
# 作用：发票源文件对象存储后端（MinIO/S3 兼容）
# 业务：设置 ENDPOINT 启用 MinIO（生产）；为空 → 本地目录替身（测试/离线）
MINIO_ENDPOINT = os.environ.get("FLOWINVOICE_MINIO_ENDPOINT", "")
MINIO_ACCESS_KEY = os.environ.get("FLOWINVOICE_MINIO_ACCESS_KEY", "")
MINIO_SECRET_KEY = os.environ.get("FLOWINVOICE_MINIO_SECRET_KEY", "")
MINIO_BUCKET = os.environ.get("FLOWINVOICE_MINIO_BUCKET", "flowinvoice")
MINIO_SECURE = os.environ.get("FLOWINVOICE_MINIO_SECURE", "0").lower() in ("1", "true", "yes")


# ===== 异步任务（Celery + Redis，docs/06 异步任务层） =====
# 作用：是否启用"提交→处理→挂起"进 Celery worker（FLOWINVOICE_ASYNC=1/true/yes 启用）
# 业务：默认同步直跑（无 Redis 依赖，测试/离线行为不变）；生产设 1 后 API 提交即返回 pending，
#       worker 执行图管线并写 submissions 状态（#52）
ASYNC_ENABLED = os.environ.get("FLOWINVOICE_ASYNC", "").lower() in ("1", "true", "yes")

# 作用：Celery broker 的 Redis 连接串（消息队列 / worker 领单）
# 业务：仅异步模式有意义；未启用时无任何依赖
REDIS_DSN = os.environ.get("FLOWINVOICE_REDIS_DSN", "redis://localhost:6379/0")


# ===== 技术日志（app/core/logging.py，docs/07） =====
# 作用：日志级别 / 目录 / 轮转大小 / 备份份数
# 业务：生产按需调级别（如 FLOWINVOICE_LOG_LEVEL=DEBUG 排查），轮转防磁盘无限增长
LOG_LEVEL = os.environ.get("FLOWINVOICE_LOG_LEVEL", "INFO").upper()
LOG_DIR = Path(os.environ.get("FLOWINVOICE_LOG_DIR", str(PROJECT_ROOT / "logs")))
LOG_MAX_BYTES = int(os.environ.get("FLOWINVOICE_LOG_MAX_BYTES", str(10 * 1024 * 1024)))
LOG_BACKUP_COUNT = int(os.environ.get("FLOWINVOICE_LOG_BACKUP_COUNT", "5"))


def ensure_dirs() -> None:
    """确保运行期目录存在"""
    # 作用：启动时建目录，防止写库/写文件时报路径不存在
    # 业务：首次运行/换机器启动均无副作用
    for d in (UPLOAD_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)
