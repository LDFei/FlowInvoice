# app/shared/policies/errors.py —— 业务异常定义
# 业务：业务层错误要结构化成可回传给上层的原因，而非静默失败（docs/AGENTS.md §8 异常闭环）
class FlowInvoiceError(Exception):
    """报销系统业务异常基类"""


class UnknownBusinessError(FlowInvoiceError):
    """未知业务方向：未注册业务 或 classify 判定失败"""

    def __init__(self, direction: str):
        self.direction = direction
        super().__init__(f"未知业务方向: {direction}")


class OcrFailedError(FlowInvoiceError):
    """发票识别失败：票面无法解析"""


class AdvanceMissingError(FlowInvoiceError):
    """缺少有效事前申请：未申请或已过期/已使用"""
