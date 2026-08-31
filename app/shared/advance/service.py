# app/shared/advance/service.py —— 事前申请（实体 / 生命周期 / 匹配）
# 业务：员工先申请出差方向+预估金额+有效期，报销时匹配；超有效期或已使用则不能再匹配（docs/02 §12）
from datetime import date, timedelta

from app.adapters.base import StorageProvider
from app.core.ids import new_bill_no
from app.shared.policies.errors import AdvanceMissingError


class AdvanceService:
    """事前申请服务：创建 / 匹配 / 标记使用"""

    def __init__(self, storage: StorageProvider, policies):
        # 作用：注入持久化与政策加载器
        self._storage = storage
        self._policies = policies

    def create(
        self,
        *,
        employee_id: str,
        direction: str,
        start_date: str,
        end_date: str,
        estimated_amount: float,
        purpose: str,
        valid_days: int | None = None,
    ) -> dict:
        """创建事前申请单（自动计算有效期）"""
        # 业务：有效期 = 申请结束日期 + 政策允许天数（差旅默认 30 天）
        policy = self._policies.load(direction)
        valid_days = valid_days or policy.get("advance_valid_days", 30)
        valid_until = (date.fromisoformat(end_date) + timedelta(days=valid_days)).isoformat()
        app = {
            "app_id": new_bill_no("ADV"),
            "employee_id": employee_id,
            "direction": direction,
            "start_date": start_date,
            "end_date": end_date,
            "valid_until": valid_until,
            "estimated_amount": estimated_amount,
            "purpose": purpose,
            "status": "active",                      # 业务：active / used / expired
            "created_at": date.today().isoformat(),
        }
        self._storage.create_advance(app)
        return app

    def match(
        self,
        *,
        employee_id: str,
        direction: str,
        on_date: str,
        app_id: str = "",
    ) -> dict:
        """匹配有效事前申请；app_id 优先，否则按 员工+方向+日期 自动匹配"""
        # 业务：报销单可显式挂 app_id；未挂则自动匹配有效申请
        if app_id:
            app = self._storage.get_advance(app_id)
            if app and self._is_usable(app, on_date):
                return app
            raise AdvanceMissingError(f"事前申请 {app_id} 不存在或已失效")
        app = self._storage.find_active_advance(employee_id, direction, on_date)
        if app is None:
            raise AdvanceMissingError("未匹配到有效事前申请，请先提交出差申请")
        return app

    def mark_used(self, app_id: str) -> None:
        """报销完成后标记已使用，防止重复使用"""
        app = self._storage.get_advance(app_id)
        if app:
            app["status"] = "used"
            self._storage.create_advance(app)  # upsert 覆盖状态

    def _is_usable(self, app: dict, on_date: str) -> bool:
        """判断申请单是否可用：active + 申请区间覆盖报销日期 + 未过有效期"""
        return (
            app["status"] == "active"
            and app["start_date"] <= on_date <= app["end_date"]
            and app["valid_until"] >= on_date
        )
