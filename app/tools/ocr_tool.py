# app/tools/ocr_tool.py —— 发票识别工具
# 业务：真实场景调用 OCR 服务识别票面；Demo 用 Mock 文本票面解析，保持工具边界一致
import re
from pathlib import Path

from app.shared.policies.errors import OcrFailedError


class OcrTool:
    """发票票面识别（Mock：解析标准文本票面；真实实现换 OCR 服务）"""

    # 作用：票面字段 → OCR 输出 key 的映射
    FIELDS = {
        "invoice_no": "发票号码",
        "invoice_type": "发票类型",
        "date": "开票日期",
        "amount": "金额",
        "title": "项目",
    }

    def extract(self, file_path: str) -> dict:
        """识别票面 → 结构化数据；失败抛 OcrFailedError（由识别节点转为结构化退回）"""
        # 业务：识别失败属于"可修复业务问题"，必须结构化成原因回传 + 可重提（docs/AGENTS.md §8）
        try:
            text = Path(file_path).read_text(encoding="utf-8")
        except Exception as exc:
            raise OcrFailedError(f"文件读取失败: {exc}") from exc

        data: dict = {}
        for key, label in self.FIELDS.items():
            # 作用：按 "字段: 值" 逐行解析票面
            m = re.search(rf"^\s*{label}\s*[:：]\s*(.+)$", text, re.MULTILINE)
            if not m:
                raise OcrFailedError(f"票面缺少字段: {label}")
            value = m.group(1).strip()
            data[key] = float(value) if key == "amount" else value
        return data
