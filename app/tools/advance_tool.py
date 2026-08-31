# app/tools/advance_tool.py —— 事前申请匹配工具
# 业务：差旅强制事前申请；无有效申请 → 结构化退回，指导报销人先申请
from app.shared.advance.service import AdvanceService
from app.shared.policies.errors import AdvanceMissingError


class AdvanceTool:
    """事前申请匹配：封装 AdvanceService，供 advance 匹配节点调用"""

    def __init__(self, service: AdvanceService):
        # 作用：注入事前申请服务
        self._service = service

    def match(
        self,
        *,
        employee_id: str,
        direction: str,
        on_date: str,
        app_id: str = "",
    ) -> dict:
        """匹配有效事前申请；找不到抛 AdvanceMissingError"""
        return self._service.match(
            employee_id=employee_id,
            direction=direction,
            on_date=on_date,
            app_id=app_id,
        )
