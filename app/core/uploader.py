# app/core/uploader.py —— 上传解析（跨业务复用的公共能力）
# 业务：所有业务（差旅/采购/招待/办公）共用此入口；业务子图只接收标准
#       InvoiceInput DTO，无需重复写文件处理（docs/AGENTS.md §3.2 核心示例）
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from app.core.config import UPLOAD_DIR


@dataclass
class InvoiceInput:
    """统一发票输入 DTO（业务无关，屏蔽来源差异）"""

    file_path: str                    # 作用：上传文件落盘路径，供 OCR 工具读取
    direction: str                    # 业务：travel/procurement... 申报时选择，供路由与事前申请匹配
    purpose: str = ""                 # 业务：报销事由，供 Agent 总结与合规判断参考
    declared_amount: float = 0.0      # 业务：申报金额（税前）；与 OCR 抽取金额比对，不一致→风险标记
    payment_method: str = "personal"  # 业务：personal=员工垫付（财务补钱）/ corporate=对公付款（付供应商）
    employee_id: str = "1001"         # 业务：申报人工号；审批链生成与通知的起点
    app_id: str = ""                  # 业务：关联的事前申请单号（差旅强制，可空→自动匹配有效申请）

    def to_dict(self) -> dict:
        """转 dict 供 State 存储"""
        # 作用：LangGraph State 只存普通 dict，不存 dataclass 对象
        return asdict(self)


class Uploader:
    """发票上传解析器（跨业务复用）"""

    def save_and_parse(self, file_storage, filename: str, **meta) -> InvoiceInput:
        """落盘文件 → 构造标准 DTO"""
        # 作用：把上传文件写到临时目录，返回统一 DTO；后续流程只认 InvoiceInput
        # 业务：屏蔽"发票来源/格式"差异（图片/PDF/电子票）；新增业务无需改动此层
        UPLOAD_DIR.mkdir(parents=True, exist_ok=True)
        # 作用：带时间戳+原文件名去重命名，避免同名覆盖
        saved = UPLOAD_DIR / f"{datetime.now():%Y%m%d%H%M%S%f}_{Path(filename).name}"
        # 作用：流式写文件（FastAPI UploadFile 是文件对象）
        with saved.open("wb") as out:
            shutil.copyfileobj(file_storage, out)
        # 作用：meta 中 employee_id/app_id/purpose 等原样透传
        return InvoiceInput(file_path=str(saved), **meta)
