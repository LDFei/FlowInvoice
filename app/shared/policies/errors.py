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


class AdvanceAmbiguousError(FlowInvoiceError):
    """自动匹配歧义：多份有效事前申请的区间都覆盖报销日期，需报销人显式指定挂哪份（#97）

    业务：不静默取最早那份（会把 A 行程的票错挂到 B 申请的预算池）；结构化退回引导指定。
    """

    def __init__(self, candidates: list[dict]):
        # 作用：候选申请（app_id/区间/事由）带进异常，退回信息可直接列给报销人
        self.candidates = candidates
        lines = "；".join(
            f"{c['app_id']}（{c.get('start_date')}~{c.get('end_date')}，{c.get('purpose', '')}）"
            for c in candidates
        )
        super().__init__(f"检测到 {len(candidates)} 份有效申请区间均覆盖该开票日期：{lines}，请指定本次票据对应的申请")
