# app/tools/email_tool.py —— 邮件工具（审批领导触达）
# 业务：审核人员确认后，Agent 把审批结论邮件触达领导；真实实现走邮件网关
from app.adapters.base import EmailProvider


class EmailTool:
    """邮件封装"""

    def __init__(self, provider: EmailProvider):
        # 作用：注入邮件实现
        self._provider = provider

    def email_leader(self, request_id: str, state: dict, leader: dict) -> None:
        """给最终审批领导发邮件（含 Agent 总结），供领导决策"""
        subject = f"[报销审批] {request_id} 待领导决策 ¥{state['invoice_data']['amount']:,.2f}"
        body = (
            f"报销人：{state['invoice_input'].get('employee_id')}\n"
            f"金额：¥{state['invoice_data']['amount']:,.2f}\n"
            f"审批链：{' → '.join(n['role'] for n in state['approval_chain'])}\n"
            f"Agent 总结：\n{state['summary']}\n"
            f"请回复批准或否决。"
        )
        self._provider.send(request_id, leader["email"], subject, body)
