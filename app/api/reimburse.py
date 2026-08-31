# app/api/reimburse.py —— 报销提交 / 审批决策 / 出纳打款 / 状态查询
# 业务：报销端上传提交、审核端/领导端决策、出纳端打款、状态查询；不含业务判断（判断在图里）
from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile

from app.api.schemas import DecideRequest, PayRequest, RequestDetail, RequestSummary
from app.api.service import decide, pay, request_view, start_reimbursement
from app.container import Container
from app.core.ids import new_request_id

router = APIRouter(prefix="/api", tags=["报销"])


def _container(request: Request) -> Container:
    # 作用：从 app.state 取装配好的容器
    return request.app.state.container


@router.post(
    "/reimburse",
    response_model=RequestDetail,
    summary="提交报销（报销端 · 第一步）",
    description="""
报销人上传发票，启动 Agent 自动闭环。

**Agent 自动完成**：识别票面 → 验真 → 匹配事前申请 → 合规检查（引用 RAG 制度依据）→ 生成审批链 → 生成审核总结 → 通知审核人员，然后**挂起等待人工复核**。

**入参**：上传发票文件 + 业务方向 + 申报金额等表单。

**返回**：`request_id`（后续审批都靠它）+ `current_step=review`（已挂起在审核复核）+ `summary`（Agent 审核总结）+ `approval_chain`（审批角色链）。

> **Demo 样例**：项目 `demo/样例-火车票.txt`，方向 `travel`、员工 `1001`、金额 `528.50`，启动服务时已种入覆盖今天的差旅事前申请，提交即可走通。
""",
)
def submit_reimburse(
    request: Request,
    file: UploadFile = File(..., description="发票文件（Demo 为文本票面）"),
    direction: str = Form("travel", description="业务方向"),
    purpose: str = Form("", description="报销事由"),
    declared_amount: float = Form(0.0, description="申报金额（税前）"),
    payment_method: str = Form("personal", description="personal=个人垫付/corporate=对公付款"),
    employee_id: str = Form("1001", description="员工工号"),
    app_id: str = Form("", description="关联事前申请单号（可空，自动匹配）"),
):
    """报销端：上传发票 → 启动 Agent 闭环（返回首个挂起点，等待审核人员复核）"""
    container = _container(request)
    # 作用：上传解析为统一 DTO（跨业务复用），再注入图
    invoice_input = container.uploader.save_and_parse(
        file.file,
        file.filename,
        direction=direction,
        purpose=purpose,
        declared_amount=declared_amount,
        payment_method=payment_method,
        employee_id=employee_id,
        app_id=app_id,
    )
    request_id = new_request_id()
    state = start_reimbursement(container, invoice_input.to_dict(), request_id)
    return request_view(container, state)


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

    # 业务：决策人必须与审批链当前步骤角色一致（越权 → 403）
    try:
        result = decide(container, request_id, body.action, body.comment, body.actor)
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc))
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
报销单列表，支持按状态过滤（`in_review` / `returned` / `approved` / `paid` / `voided`）。

报销端"我的单据"列表、审核端"待办"列表、管理端驾驶舱的数据源。
""",
)
def list_requests(request: Request, status: str | None = None):
    """报销单列表（可按状态过滤；管理端/驾驶舱数据源）"""
    return _container(request).storage.list_requests(status)
