# app/api/advance.py —— 事前申请接口
# 业务：报销前先创建事前申请单（方向+预估金额+有效期）；差旅报销时按它匹配（docs/02 §12）
from datetime import date

from fastapi import APIRouter, Request

from app.api.schemas import AdvanceCreate, AdvanceDetail

router = APIRouter(prefix="/api", tags=["事前申请"])


def _effective_status(app: dict) -> str:
    """读路径派生（#104）：申请单状态按有效期实时判定，不物理写 expired

    业务：预算池模型下 DB 只存 active（创建恒 active、永不自动置 expired，docs/14 P-3 建议"删掉对
    已存 expired 状态的承诺"）——匹配有效性本就由 find_active_advances 查询时的 valid_until 日期门
    实时判定。列表接口在此把"已过有效期"的 active 派生为 expired 展示，前端 Tag/状态过滤据此生效；
    避免另起后台清扫任务去写死状态（派生永不漂移、无双实现状态源）。
    """
    if app.get("status") == "active" and app.get("valid_until", "") < date.today().isoformat():
        return "expired"
    return app.get("status", "")


@router.post(
    "/advance",
    response_model=AdvanceDetail,
    summary="创建事前申请单",
    description="""
出差前先提交事前申请（**差旅报销的前置条件**——没有有效申请单，报销会直接退回）。

创建后自动计算**有效期**（结束日期 + 政策天数，默认 30 天）。报销时系统按 **员工工号 + 业务方向 + 开票日期** 自动匹配：日期落在 `[start_date, end_date]` 区间且未过期才有效。

> Demo 启动时已自动种入一份员工 1001 的差旅申请（覆盖今天 ±4 天），所以直接走报销也能匹配成功；本接口用于新增更多申请。
""",
)
def create_advance(request: Request, body: AdvanceCreate):
    """创建事前申请单（返回含自动计算的有效期）"""
    container = request.app.state.container
    return container.advances.create(**body.model_dump())


@router.get(
    "/advances",
    response_model=list[AdvanceDetail],
    summary="事前申请列表",
    description="""
事前申请单列表，支持按状态（`active` / `expired`）与员工过滤；每条附**预算占用信息**（`reserved_amount` 已报合计、`remaining_amount` 剩余额度）。

状态按**有效期实时判定**（#104）：`active`=未过 `valid_until`；`expired`=已过有效期（数据库不物理写 expired，匹配有效性由查询日期门保证）。

报销端"我申请的事前单"、报销时选关联申请、审核端核对申请有效性时使用。
""",
)
def list_advances(request: Request, status: str | None = None, employee_id: str | None = None):
    """事前申请列表（可按派生状态/员工过滤，附预算占用：#91 预算池前端展示）

    状态语义（#104）：status 参数按 _effective_status 实时派生过滤——active=未过有效期、
    expired=已过 valid_until（DB 只存 active，不再维护物理 expired 状态）。后端不过滤再在
    Python 层派生：本地 demo 申请单量级小（数十~百级），全量取回内存过滤语义清晰、无双实现状态源。
    """
    container = request.app.state.container
    # 作用：storage.list_advances() 取全量（不传 status），派生与过滤全在 Python 层一处完成
    items = container.storage.list_advances()
    out = []
    for a in items:
        eff = _effective_status(a)
        if status and eff != status:
            continue
        if employee_id and a["employee_id"] != employee_id:
            continue
        out.append({**a, "status": eff})
    # 作用：占用信息=预算池台账实时合计（reserve_advance 原子写入，读时不额外加锁）
    return [
        {
            **a,
            "reserved_amount": container.advances.reserved(a["app_id"]),
            "remaining_amount": round(a["estimated_amount"] - container.advances.reserved(a["app_id"]), 2),
        }
        for a in out
    ]
