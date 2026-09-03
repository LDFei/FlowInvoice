# app/api/advance.py —— 事前申请接口
# 业务：报销前先创建事前申请单（方向+预估金额+有效期）；差旅报销时按它匹配（docs/02 §12）
from fastapi import APIRouter, Request

from app.api.schemas import AdvanceCreate, AdvanceDetail

router = APIRouter(prefix="/api", tags=["事前申请"])


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

报销端"我申请的事前单"、报销时选关联申请、审核端核对申请有效性时使用。
""",
)
def list_advances(request: Request, status: str | None = None, employee_id: str | None = None):
    """事前申请列表（可按状态/员工过滤，附预算占用：#91 预算池前端展示）"""
    container = request.app.state.container
    items = container.storage.list_advances(status)
    if employee_id:
        items = [a for a in items if a["employee_id"] == employee_id]
    # 作用：占用信息=预算池台账实时合计（reserve_advance 原子写入，读时不额外加锁）
    return [
        {
            **a,
            "reserved_amount": container.advances.reserved(a["app_id"]),
            "remaining_amount": round(a["estimated_amount"] - container.advances.reserved(a["app_id"]), 2),
        }
        for a in items
    ]
