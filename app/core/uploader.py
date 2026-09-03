# app/core/uploader.py —— 上传解析（跨业务复用的公共能力）
# 业务：所有业务（差旅/采购/招待/办公）共用此入口；业务子图只接收标准
#       InvoiceInput DTO，无需重复写文件处理（docs/AGENTS.md §3.2 核心示例）
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from app.adapters.base import ObjectStorage
from app.core.config import ALLOWED_UPLOAD_EXTENSIONS, MAX_UPLOAD_BYTES, UPLOAD_DIR, UPLOAD_RETENTION_DAYS

# 作用：分块读取缓冲（读一块写一块，边读边计数，超限立即中止不落盘）
_CHUNK_SIZE = 64 * 1024


class UploadValidationError(Exception):
    """上传文件校验失败（类型/大小不合法），路由层转 400"""
    # 业务：上传入口的输入校验错误属于"请求参数问题"（4xx），不是服务端故障（5xx），
    #       单独异常类型便于 API 层精确映射状态码而非 500


@dataclass
class InvoiceInput:
    """统一发票输入 DTO（业务无关，屏蔽来源差异）"""

    file_path: str                    # 作用：上传文件落盘路径（本地临时，供 OCR 工具读取）
    object_key: str = ""              # 作用：源文件对象存储 key（MinIO/本地替身，持久副本，DB 存它）
    direction: str = ""               # 业务：费用类型组（内部路由键，非用户选择；当前 travel=差旅），供路由与事前申请匹配
    purpose: str = ""                 # 业务：报销事由，供 Agent 总结与合规判断参考
    declared_amount: float = 0.0      # 业务：申报金额（税前）；与 OCR 抽取金额比对，不一致→风险标记
    payment_method: str = "personal"  # 业务：personal=员工垫付（财务补钱）/ corporate=对公付款（付供应商）
    employee_id: str = "1001"         # 业务：申报人工号；审批链生成与通知的起点
    mode: str = "advance"             # 业务：报销模式——advance=关联事前申请（默认，差旅行程票挂到已批出差申请）/ direct=直接报销（未做事前申请，凭票直报、不进预算池）
    app_id: str = ""                  # 业务：关联的事前申请单号（advance 模式可空→唯一命中自动挂靠，显式指定以它为准；direct 模式恒空）
    parent_request_id: str = ""       # 业务：#89 退回重提留痕——原报销单号（若本次由某单退回/作废后重提）

    def to_dict(self) -> dict:
        """转 dict 供 State 存储"""
        # 作用：LangGraph State 只存普通 dict，不存 dataclass 对象
        return asdict(self)


class Uploader:
    """发票上传解析器（跨业务复用）"""

    def __init__(self, object_storage: ObjectStorage):
        # 作用：注入对象存储——源文件持久副本进对象存储，本地临时文件仅供 OCR 读取（docs/06 Phase 2）
        self._objects = object_storage

    def save_and_parse(self, file_storage, filename: str, **meta) -> InvoiceInput:
        """持久化源文件 → 落 OCR 临时文件 → 构造标准 DTO"""
        # 业务：屏蔽"发票来源/格式"差异（图片/PDF/电子票）；新增业务无需改动此层。
        #       本地临时文件（UPLOAD_DIR）是处理缓存，源文件权威副本在对象存储（file_key 可重建）
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        # 作用：带时间戳+原文件名去重命名，避免同名覆盖
        key = f"{datetime.now():%Y%m%d%H%M%S%f}_{Path(filename).name}"
        saved = UPLOAD_DIR / key
        # 作用：扩展名白名单校验——白名单外直接拒绝，不落盘（接收未知类型文件是上传入口安全面）
        ext = Path(filename).suffix.lower()
        if ext not in ALLOWED_UPLOAD_EXTENSIONS:
            raise UploadValidationError(
                f"不支持的文件类型 {ext or '(无扩展名)'}，允许: {', '.join(sorted(ALLOWED_UPLOAD_EXTENSIONS))}"
            )
        # 作用：分块流式写本地临时文件（FastAPI UploadFile 是文件对象）；边写边计数，
        #       超上限即删临时文件并拒绝——超大文件不会整份落盘打满磁盘
        try:
            with saved.open("wb") as out:
                total = 0
                while chunk := file_storage.read(_CHUNK_SIZE):
                    total += len(chunk)
                    if total > MAX_UPLOAD_BYTES:
                        raise UploadValidationError(
                            f"文件超过大小上限 {MAX_UPLOAD_BYTES / (1024 * 1024):.0f}MB"
                        )
                    out.write(chunk)
        except UploadValidationError:
            saved.unlink(missing_ok=True)  # 超限已部分落盘 → 清理半成品，不留垃圾
            raise
        # 作用：源文件持久副本进对象存储（幂等覆盖；MinIO 桶 / 本地目录）
        self._objects.put(key, saved.read_bytes())
        # 作用：meta 中 employee_id/app_id/purpose 等原样透传
        return InvoiceInput(file_path=str(saved), object_key=key, **meta)

    def cleanup_stale(self, days: int = UPLOAD_RETENTION_DAYS) -> int:
        """清理上传临时目录中超 N 天的处理缓存文件（启动时 sweep）"""
        # 业务：源文件权威副本在对象存储（DB 只存 file_key），本地临时文件仅供 OCR 读取一次即废——
        #       不清理则 data/uploads 随提交量无限增长。只删过期文件，不碰 7 天内可能仍在重试/重提的文件
        cutoff = datetime.now().timestamp() - days * 86400
        count = 0
        for p in UPLOAD_DIR.glob("*"):
            try:
                if p.is_file() and p.stat().st_mtime < cutoff:
                    p.unlink()
                    count += 1
            except OSError:
                continue  # 文件被占用/刚被删 → 跳过，下次 sweep 再收
        return count
