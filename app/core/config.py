# app/core/config.py —— 全局路径与基础配置
# 业务：统一项目根路径/数据目录/上传目录/策略目录，避免各模块各自拼接路径
from pathlib import Path

# 作用：项目根目录 = 本文件向上两级（app/core -> app -> 项目根）
# 业务：无论从哪个目录启动服务，路径都稳定指向项目根
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 作用：运行期数据目录（数据库 / 上传临时文件）
# 业务：本地 Demo 落盘于此；后续可替换为对象存储 / 独立数据库
DATA_DIR = PROJECT_ROOT / "data"

# 作用：上传文件临时目录
# 业务：Uploader 落盘后交给 OCR 工具读取
UPLOAD_DIR = DATA_DIR / "uploads"

# 作用：SQLite 数据库文件
# 业务：请求/事前申请/消息/邮件统一持久化于此
DB_PATH = DATA_DIR / "flowinvoice.db"

# 作用：政策规则 YAML 目录
# 业务：一个业务一个 YAML（docs/AGENTS.md §9 配置驱动）
POLICY_DIR = PROJECT_ROOT / "app" / "policy"

# 作用：制度条款语料目录（非结构化政策文本，RAG 检索源）
# 业务：差旅/招待/票据/通用原则等条款，按场景检索作"依据"（docs/03 §3）
CLAUSES_DIR = POLICY_DIR / "clauses"

# 作用：是否启用制度条款 RAG 检索
# 业务：可整体开关；关闭时合规节点只做确定性检查
RAG_ENABLED = True

# 作用：每次检索返回的条款条数
RAG_TOP_K = 3

# 作用：金额误差容差（申报金额 vs 票面金额 差异比例）
# 业务：超过该比例 → 打风险标记，提示申报人填写有误
AMOUNT_DIFF_THRESHOLD = 0.05

# 作用：演示员工号（Demo 不接登录，固定申报人）
# 业务：真实系统由登录态注入；此处简化便于跑通闭环
DEMO_EMPLOYEE_ID = "1001"


def ensure_dirs() -> None:
    """确保运行期目录存在"""
    # 作用：启动时建目录，防止写库/写文件时报路径不存在
    # 业务：首次运行/换机器启动均无副作用
    for d in (UPLOAD_DIR, DATA_DIR):
        d.mkdir(parents=True, exist_ok=True)
