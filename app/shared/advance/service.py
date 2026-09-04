# app/shared/advance/service.py —— 事前申请（实体 / 生命周期 / 匹配）
# 业务：申请=一趟差旅的预算额度（预估金额）——出差多张票据各自提交报销、共享同一申请，
#       逐笔按票面金额累计占用（预算台账 advance_reservations）；日期落在区间内即可匹配，
#       超预算不阻断、由复核人特批（软闸门）。申请是否可用只由 状态+区间+有效期 决定（docs/02 §12）。
from datetime import date, timedelta

from app.adapters.base import StorageProvider
from app.core.ids import new_bill_no
from app.shared.policies.errors import AdvanceAmbiguousError, AdvanceMissingError


class AdvanceService:
    """事前申请服务：创建 / 匹配 / 预算占用（台账核销，docs/02 §12 / docs/06 预算台账）"""

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
            "status": "active",                      # 业务：active / expired；used 不再由报销自动置（预算池模型）
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
        # 业务：报销单可显式挂 app_id；未挂则自动匹配。自动匹配只认"唯一命中"——
        #       多份申请区间重叠覆盖该日期时不猜（曾静默取最早，会把 A 行程的票错挂到 B 的预算池），
        #       抛歧义异常 → 图节点结构化退回，报销人用提交页「关联事前申请」显式指定（#97）。
        if app_id:
            app = self._storage.get_advance(app_id)
            if app and self._is_usable(app, on_date):
                return self._with_reserved(app)
            raise AdvanceMissingError(f"事前申请 {app_id} 不存在或已失效")
        apps = self._storage.find_active_advances(employee_id, direction, on_date)
        if not apps:
            raise AdvanceMissingError("未匹配到有效事前申请，请先提交出差申请")
        if len(apps) > 1:
            raise AdvanceAmbiguousError(apps)
        return self._with_reserved(apps[0])

    def match_dates(
        self,
        *,
        employee_id: str,
        direction: str,
        dates: list[str],
        app_id: str = "",
    ) -> dict:
        """#A 多票批的请求级匹配：一张申请必须覆盖整批被接受票的全部开票日期

        语义与 match 逐字节一致（单票 dates=[d] 时完全等价）：
        - 显式 app_id：须对所有日期可用，否则 advance_missing；
        - 自动匹配：按日期求候选申请交集（同一申请需覆盖全部日期才可能挂靠）；
          交集唯一 → 命中；空 → advance_missing；多份重叠 → advance_ambiguous（不静默取最早，保 #97）。
        批准口径：一批票=一趟差旅共享同一申请/预算池，故整批只匹配一份申请。
        """
        # 作用：去重保序（同一日期两张票仍按一份区间判定），避免重复查询
        unique = sorted({d for d in dates if d})
        if app_id:
            app = self._storage.get_advance(app_id)
            if app and all(self._is_usable(app, d) for d in unique):
                return self._with_reserved(app)
            raise AdvanceMissingError(f"事前申请 {app_id} 不存在或已失效")
        common: dict | None = None
        for d in unique:
            apps = {a["app_id"]: a for a in self._storage.find_active_advances(employee_id, direction, d)}
            if not apps:
                raise AdvanceMissingError("未匹配到有效事前申请，请先提交出差申请")
            common = apps if common is None else {k: v for k, v in common.items() if k in apps}
            if not common:
                break  # 任一日期无共同覆盖 → 无申请能覆盖整批
        if not common:
            raise AdvanceMissingError("未匹配到有效事前申请，请先提交出差申请")
        if len(common) > 1:
            raise AdvanceAmbiguousError(list(common.values()))
        return self._with_reserved(next(iter(common.values())))

    def reserve(self, app_id: str, request_id: str, amount: float) -> float:
        """报销单 approved/paid 终态 → 按票面金额占一笔预算（advance_reservations 台账）"""
        # 业务：批准即占用（流程终态不可退回），paid 兜底幂等；request_id 唯一——同一报销单被
        #       approve+paid 双入口重复核销只累计一次。申请状态不翻 used（仍 active）：一次出差
        #       多张票共享同一申请逐笔累计占用，直到申请区间/有效期自然结束（预算池，docs/02 §12）。
        return self._storage.reserve_advance(app_id, amount, request_id)

    def reserved(self, app_id: str) -> float:
        """某事前申请的累计占用额（已 approved 报销单按票面金额合计，预算执行视图用）"""
        return self._storage.sum_advance_reservations(app_id)

    def _with_reserved(self, app: dict) -> dict:
        """把台账累计占用额并入申请单返回（submit 时点快照，供预算软闸门与前端展示）"""
        return {**app, "reserved_amount": self._storage.sum_advance_reservations(app["app_id"])}

    def _is_usable(self, app: dict, on_date: str) -> bool:
        """判断申请单是否可用：active + 申请区间覆盖报销日期 + 未过有效期"""
        return (
            app["status"] == "active"
            and app["start_date"] <= on_date <= app["end_date"]
            and app["valid_until"] >= on_date
        )
