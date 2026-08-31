# app/tools/notify_tool.py —— 通知工具（审核人 / 报销人 / 全链 / 财务）
# 业务：给不同角色的通知内容与时机不同；写库 + 打印（真实实现对接 IM/站内信）
from app.adapters.base import NotifyProvider


class NotifyTool:
    """通知封装"""

    def __init__(self, provider: NotifyProvider):
        # 作用：注入通知实现
        self._provider = provider

    def notify_reviewer(self, request_id: str, state: dict) -> None:
        """给第一位审批人发审核总结（人工复核的输入）"""
        first = state["approval_chain"][0]
        title = f"待审核报销单 {request_id}（{state['business_type']}）"
        content = (
            f"报销人 {state['invoice_input'].get('employee_id')}，"
            f"金额 ¥{state['invoice_data']['amount']:,.2f}。\n"
            f"Agent 审核总结：\n{state['summary']}"
        )
        self._provider.send(request_id, first["role"], title, content)

    def notify_submitter(self, request_id: str, state: dict) -> None:
        """通知报销人退回原因（可重提）"""
        reason = state.get("return_reason", {})
        title = f"报销单 {request_id} 已退回"
        content = f"原因：{reason.get('message', '')}\n建议：{reason.get('suggestion', '')}"
        self._provider.send(request_id, f"报销人 {state['invoice_input'].get('employee_id')}", title, content)

    def notify_all_chain(self, request_id: str, state: dict) -> None:
        """通知全部审批链角色作废原因（最终否决 → 全员知晓）"""
        reason = state.get("return_reason", {}) or {}
        for node in state.get("approval_chain", []):
            title = f"报销单 {request_id} 已作废"
            content = f"作废原因：{reason.get('message', '最终审核未通过')}\n建议：{reason.get('suggestion', '')}"
            self._provider.send(request_id, node["role"], title, content)

    def notify_finance(self, request_id: str, state: dict) -> None:
        """通知财务（出纳）付款：审批≠支付，付款仅财务执行"""
        title = f"报销单 {request_id} 已批准，待付款"
        content = (
            f"金额 ¥{state['invoice_data']['amount']:,.2f}，"
            f"支付方式 {state['invoice_input'].get('payment_method')}，"
            f"请按财务流程处理。"
        )
        self._provider.send(request_id, "财务", title, content)

    def notify_gm(self, request_id: str, state: dict) -> None:
        """小额报销告知：总经理未参与审批，批准后仅通知知悉（不占审批资源）"""
        # 业务：制度规定小额免总经理审批，但管理层需知情，故批准后发一条告知留痕
        title = f"报销单 {request_id} 已批准（小额告知）"
        content = (
            f"报销人 {state['invoice_input'].get('employee_id')}，"
            f"金额 ¥{state['invoice_data']['amount']:,.2f}，"
            f"已由直属上级审批通过，即将由财务出纳打款，总经理知悉即可。"
        )
        self._provider.send(request_id, "总经理", title, content)

    def notify_paid(self, request_id: str, state: dict) -> None:
        """通知报销人：财务已打款到账"""
        title = f"报销单 {request_id} 已打款"
        content = (
            f"金额 ¥{state['invoice_data']['amount']:,.2f}，"
            f"已由财务出纳打款，请注意查收。"
        )
        self._provider.send(request_id, f"报销人 {state['invoice_input'].get('employee_id')}", title, content)
