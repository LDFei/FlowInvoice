# app/api/reimburse.py —— 报销提交 / 审批决策 / 出纳打款 / 状态查询
# 业务：报销端上传提交、审核端/领导端决策、出纳端打款、状态查询；不含业务判断（判断在图里）
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse

from app.api.schemas import DecideRequest, PayRequest, RequestDetail, RequestSummary, SubmissionStatus
from app.api.service import decide, pay, request_view, start_reimbursement
from app.container import Container
from app.core.config import ASYNC_ENABLED
from app.core.ids import new_request_id
from app.core.uploader import UploadValidationError

router = APIRouter(prefix="/api", tags=["报销"])


def _container(request: Request) -> Container:
    # 作用：从 app.state 取装配好的容器
    return request.app.state.container


@router.post(
    "/reimburse",
    response_model=RequestDetail,
    responses={202: {"model": SubmissionStatus, "description": "异步模式（FLOWINVOICE_ASYNC=1）：提交受理，worker 处理中，前端轮询任务状态"}},
    summary="提交报销（报销端 · 第一步）",
    description="""
报销人上传发票，启动 Agent 自动闭环。

**Agent 自动完成**：识别票面 → 验真 → 匹配事前申请 → 合规检查（引用 RAG 制度依据）→ 生成审批链 → 生成审核总结 → 通知审核人员，然后**挂起等待人工复核**。

**入参**：上传发票文件 + 申报金额 + 报销模式 + 关联出差申请（可空，自动匹配）等表单；`direction` 为系统内部费用类型组路由键（当前固定 `travel`=差旅费用组，用户端不选择，多组并存后由系统按票据归类）。

**报销模式**（`mode`）：默认 `advance`=关联事前申请（差旅行程票挂到已批出差申请、占用预算池）；选 `direct`=直接报销（未做事前申请，凭票直报、不进预算池）。

**返回**：`request_id`（后续审批都靠它）+ `current_step=review`（已挂起在审核复核）+ `summary`（Agent 审核总结）+ `approval_chain`（审批角色链）。

**异步模式**：启用 `FLOWINVOICE_ASYNC=1` 后提交返回 `202` + 任务状态（pending），实际处理在 Celery worker 中进行。

> **Demo 样例**：项目 `demo/样例-火车票.txt`，方向 `travel`、员工 `1001`、金额 `528.50`，启动服务时已种入覆盖今天的差旅事前申请，提交即可走通。
""",
)
def submit_reimburse(
    request: Request,
    file: UploadFile = File(..., description="发票文件（Demo 为文本票面）"),
    direction: str = Form("travel", description="费用类型组（内部路由键；当前仅差旅 travel=个人报销首个实例）"),
    purpose: str = Form("", description="报销事由"),
    declared_amount: float = Form(0.0, description="申报金额（税前）"),
    payment_method: str = Form("personal", description="personal=个人垫付/corporate=对公付款"),
    employee_id: str = Form("1001", description="员工工号"),
    mode: str = Form("advance", description="报销模式：advance=关联事前申请（默认）/ direct=直接报销（不关联，不进预算池）"),
    app_id: str = Form("", description="关联事前申请单号（advance 模式可空自动匹配；direct 模式忽略）"),
    parent_request_id: str = Form("", description="退回重提留痕：原报销单号（该单须已退回/作废）"),
):
    """报销端：上传发票 → 启动 Agent 闭环（返回首个挂起点，等待审核人员复核）"""
    container = _container(request)
    # 作用：#89 退回重提留痕——关联原单仅限已退回/作废（发票池已释放、可改后重提）；
    #       校验放上传前，避免无效关联时白落盘
    if parent_request_id:
        parent = container.storage.get_request(parent_request_id)
        if parent is None:
            raise HTTPException(status_code=400, detail=f"关联的原报销单不存在: {parent_request_id}")
        if parent.get("process_status") not in ("returned", "voided"):
            raise HTTPException(
                status_code=400,
                detail=f"仅可关联已退回/已作废的原报销单（当前状态: {parent.get('process_status', '')}）",
            )
    # 作用：上传解析为统一 DTO（跨业务复用），再注入图；文件类型/大小非法 → 400（#65）
    try:
        invoice_input = container.uploader.save_and_parse(
            file.file,
            file.filename,
            direction=direction,
            purpose=purpose,
            declared_amount=declared_amount,
            payment_method=payment_method,
            employee_id=employee_id,
            mode=mode,
            app_id=app_id,
            parent_request_id=parent_request_id,
        )
    except UploadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request_id = new_request_id()
    result = start_reimbursement(container, invoice_input.to_dict(), request_id)
    # 作用：异步模式提交即返回 202 + 任务状态（worker 处理中，前端轮询 #53）；
    #       同步模式返回完整详情（历史行为不变）；入队失败降级同步时仍 202，任务行已对齐实际结果
    if ASYNC_ENABLED:
        return JSONResponse(status_code=202, content=result)
    return request_view(container, result)


@router.get(
    "/submissions/{request_id}",
    response_model=SubmissionStatus,
    summary="查询异步提交任务状态（提交后轮询用）",
    description="""
异步模式（`FLOWINVOICE_ASYNC=1`）下 `POST /api/reimburse` 返回 `202` + pending 任务凭证，
前端用本接口轮询任务状态：`pending`（排队中）→ `processing`（处理中）→ `succeeded`（已完成，
单据已写入，可再查 `/api/requests/{request_id}`）或 `failed`（重试耗尽，附失败原因）。

同步模式（默认）提交即返回完整详情，不会产生任务行 → 本接口 404。
""",
)
def get_submission_status(request: Request, request_id: str):
    """任务状态查询：pending/processing/succeeded/failed + 重试次数 + 失败原因（供前端轮询/展示）"""
    container = _container(request)
    sub = container.storage.get_submission(request_id)
    if sub is None:
        raise HTTPException(status_code=404, detail="异步提交任务不存在（同步模式提交不会产生任务行）")
    # 作用：显式映射字段，避免存储行多余键（snapshot 等）误入响应
    return SubmissionStatus(
        request_id=request_id,
        status=sub.get("status", "pending"),
        attempts=sub.get("attempts", 0),
        error=sub.get("error"),
        created_at=sub.get("created_at", ""),
        updated_at=sub.get("updated_at", ""),
    )


@router.post(
    "/requests/{request_id}/decide",
    response_model=RequestDetail,
    summary="审批决策（审核端 / 领导端）",
    description="""
恢复挂起的人工审批并提交决策。**允许的动作随当前步骤自动校验**（防止越权）：

| 当前步骤 | 可用动作 | 结果 |
|---|---|---|
| `review`（审核人员复核） | `approve` | 批准 → 挂起到领导决策（并给领导发邮件） |
| `review`（审核人员复核） | `return` | 退回 → 报销单 `returned`，通知报销人修改重提 |
| `leader_decision`（领导决策） | `approve` | 最终批准 → 单据 `approved`，通知财务出纳付款 |
| `leader_decision`（领导决策） | `void` | 作废 → 单据 `voided`，通知全部审批链角色 |

**入参**：`action`（approve / return / void）、`comment`（意见，退回/作废时填原因）、`actor`（决策人工号，Demo 直接传，真实系统从登录态获取）。

**返回**：决策后的最新状态（步骤推进 / 终态 + 通知留痕）。
""",
)
def decide_request(
    request: Request,
    request_id: str,
    body: DecideRequest,
):
    """审批决策：审核人员 approve/return；领导 approve/void（按当前步骤校验动作合法性）"""
    container = _container(request)
    state = container.storage.get_request(request_id)
    if state is None:
        raise HTTPException(status_code=404, detail="报销单不存在")

    step = state.get("current_step", "")
    # 业务：不同挂起点只允许对应动作，防止越权操作（如领导还没决策前审核人员先作废）
    allowed = {
        "review": {"approve", "return"},
        "leader_decision": {"approve", "void"},
    }.get(step, set())
    if body.action not in allowed:
        raise HTTPException(
            status_code=400,
            detail=f"当前步骤({step})不允许操作 {body.action}，允许: {sorted(allowed) or '无'}",
        )

    # 业务：决策人必须与审批链当前步骤角色一致（越权 → 403）；
    #       批准软闸门未通过项须填意见（#88，decide 抛 ValueError → 400，非 500）
    try:
        result = decide(container, request_id, body.action, body.comment, body.actor)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return request_view(container, result)


@router.post(
    "/requests/{request_id}/pay",
    response_model=RequestDetail,
    summary="出纳打款（出纳端 · 审批通过后）",
    description="""
出纳在**审批通过（`approved`）**后确认打款，单据转为 `paid` 并通知报销人到账。

**业务边界**：审批≠支付——审批链角色只批准不碰钱；打款是财务域动作，仅出纳（角色"财务"）可执行，越权 → 403。

**入参**：`comment`（打款备注，如转账流水号）、`actor`（出纳工号，Demo 直接传 3001）。

**返回**：打款后的最新状态（`status=paid` + `payment` 打款记录 + 站内通知留痕）。
""",
)
def pay_request(
    request: Request,
    request_id: str,
    body: PayRequest,
):
    """出纳打款：approved → paid（校验单据存在/状态，仅限出纳角色）"""
    container = _container(request)
    try:
        result = pay(container, request_id, body.comment, body.actor)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
    return request_view(container, result)


@router.get(
    "/requests/{request_id}",
    response_model=RequestDetail,
    summary="查询报销单详情",
    description="""
查看单个报销单的完整状态：流程进度、Agent 审核总结、合规检查结果、审批链、以及所有**通知 / 邮件留痕**。

报销端刷新"我的单据"、审核端加载复核页都用它。`paused=true` 表示当前正等着某个人操作。
""",
)
def get_request_detail(request: Request, request_id: str):
    """状态查询：报销单全量视图（含通知/邮件留痕）"""
    container = _container(request)
    state = container.storage.get_request(request_id)
    if state is None:
        raise HTTPException(status_code=404, detail="报销单不存在")
    return request_view(container, state)


@router.get(
    "/requests",
    response_model=list[RequestSummary],
    summary="报销单列表",
    description="""
报销单列表，支持按状态 / 申报人 / 待办审批人过滤（`in_review` / `returned` / `approved` / `paid` / `voided`）。

- `employee_id`：只看某申报人的单据（报销端"我的单据"）
- `approver_id`：只看当前步骤待该人审批的单据（审核端/领导端"待办"；review 步=链首审批人，leader_decision 步=链尾决策人）

报销端"我的单据"、审核端"待办"、管理端驾驶舱的数据源。
""",
)
def list_requests(
    request: Request,
    status: str | None = None,
    employee_id: str | None = None,
    approver_id: str | None = None,
):
    """报销单列表（可按状态/申报人/待办审批人过滤；#70 数据隔离）"""
    return _container(request).storage.list_requests(
        status, employee_id=employee_id, approver_id=approver_id,
    )
