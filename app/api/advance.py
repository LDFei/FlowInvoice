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
事前申请单列表，支持按状态过滤（`active` / `used` / `expired`）。

报销端"我申请的事前单"、审核端核对申请是否有效时使用。
""",
)
def list_advances(request: Request, status: str | None = None):
    """事前申请列表（可按状态过滤）"""
    return request.app.state.container.storage.list_advances(status)
