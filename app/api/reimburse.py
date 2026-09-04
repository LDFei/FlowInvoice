# app/api/reimburse.py —— 报销提交 / 审批决策 / 出纳打款 / 状态查询
# 业务：报销端上传提交、审核端/领导端决策、出纳端打款、状态查询；不含业务判断（判断在图里）
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, Response

from app.api.schemas import DecideRequest, PayRequest, RequestDetail, RequestSummary, SubmissionStatus
from app.api.service import _live_state, _submission_status, decide, pay, request_view, retry_submission, start_reimbursement
from app.container import Container
from app.core.config import ASYNC_ENABLED
from app.core.ids import new_request_id
from app.core.uploader import UploadValidationError

router = APIRouter(prefix="/api", tags=["报销"])

# 原件预览/下载：按扩展名定媒体类型（图片/PDF 内联预览，其余浏览器按下载处理）
# 业务：发票源文件权威副本在对象存储（docs/06 §5），预览/下载从对象存储取，不依赖本地临时缓存
_CONTENT_TYPES = {
    ".pdf": "application/pdf",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".bmp": "image/bmp",
    ".webp": "image/webp",
    ".xml": "application/xml; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".ofd": "application/vnd.ofd",
}

# 作用：#A 一报销单（request_id）可容纳的最大票数（前端同限；超限 400）
MAX_BATCH_FILES = 10


def _snapshot_employee(snapshot) -> str:
    """从任务快照取申报人工号：#A 快照=list[每票 meta]（首票=请求级共享字段源）；旧任务为单 dict"""
    if isinstance(snapshot, list):
        return (snapshot[0] or {}).get("employee_id", "") if snapshot else ""
    return (snapshot or {}).get("employee_id", "")


def _container(request: Request) -> Container:
    # 作用：从 app.state 取装配好的容器
    return request.app.state.container


def _submit_uploads(container, uploads: list[UploadFile], meta: dict) -> list[dict]:  # 纯助手：非路由
    """逐个上传文件 → 对象存储持久化 + 解析为标准输入 DTO 列表（每元素 = 单张票的上传 meta）"""
    inputs = []
    for uf in uploads:
        parsed = container.uploader.save_and_parse(uf.file, uf.filename, **meta)
        item = parsed.to_dict()
        # 作用：把原始文件名透传给批引擎（被拒票展示名用；DTO 无此字段，挂在自由 dict 上）
        item["file_name"] = uf.filename
        inputs.append(item)
    return inputs


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
    file: UploadFile | None = File(None, description="单张发票文件（与 files 二选一；兼容单票调用/旧客户端）"),
    files: list[UploadFile] | None = File(None, description="#A 批量发票（1..10 张，与 file 二选一）"),
    direction: str = Form("travel", description="费用类型组（内部路由键；当前仅差旅 travel=个人报销首个实例）"),
    purpose: str = Form("", description="报销事由"),
    declared_amount: float = Form(0.0, description="申报金额（税前）；单票=该票申报额；多票留 0 按票面申报（与票面比对见风险标记）"),
    payment_method: str = Form("personal", description="personal=个人垫付/corporate=对公付款"),
    employee_id: str = Form("1001", description="员工工号"),
    mode: str = Form("advance", description="报销模式：advance=关联事前申请（默认）/ direct=直接报销（不关联，不进预算池）"),
    app_id: str = Form("", description="关联事前申请单号（advance 模式可空自动匹配；direct 模式忽略）"),
    parent_request_id: str = Form("", description="退回重提留痕：原报销单号（该单须已退回/作废）"),
):
    """报销端：上传发票（1..10 张）→ 启动 Agent 闭环（返回首个挂起点，等待审核人员复核）"""
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
    # 作用：兼容两种调用面（file 单票 / files 多票）；两者皆空 → 400
    uploads = files or ([file] if file is not None else [])
    if not uploads:
        raise HTTPException(status_code=400, detail="请至少上传一张发票文件")
    if len(uploads) > MAX_BATCH_FILES:
        raise HTTPException(status_code=400, detail=f"单次最多提交 {MAX_BATCH_FILES} 张发票，实际 {len(uploads)} 张")
    # 作用：多票批不做逐票申报额比对（用户按票面申报），declared 只对单票调用面生效（差异比对仍逐票）
    meta = {
        "direction": direction,
        "purpose": purpose,
        "payment_method": payment_method,
        "employee_id": employee_id,
        "mode": mode,
        "app_id": app_id,
        "parent_request_id": parent_request_id,
        "declared_amount": declared_amount if len(uploads) == 1 else 0.0,
    }
    # 作用：上传解析为统一 DTO 列表（跨业务复用），再注入图；文件类型/大小非法 → 400（#65）
    try:
        invoice_inputs = _submit_uploads(container, uploads, meta)
    except UploadValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    request_id = new_request_id()
    result = start_reimbursement(container, invoice_inputs, request_id)
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
    return SubmissionStatus(**_submission_status(sub))


@router.get(
    "/submissions",
    response_model=list[SubmissionStatus],
    summary="异步提交任务列表（报销端可见，可按申报人/状态过滤）",
    description="""
异步模式任务列表——报销端"我的单据"展示**处理失败的任务**用（失败任务不产生报销单，
只有这里能看见，可重试）。

- `employee_id`：只看某申报人的任务（submissions.snapshot 内含申报人工号）
- `status`：`pending` / `processing` / `succeeded` / `failed`

同步模式提交不产生任务行 → 列表为空。
""",
)
def list_submission_status(
    request: Request,
    employee_id: str | None = None,
    status: str | None = None,
):
    """任务列表（按 snapshot 内申报人过滤——任务行无独立 employee 列，快照即权威）"""
    container = _container(request)
    rows = container.storage.list_submissions(status=status)
    if employee_id:
        rows = [r for r in rows if _snapshot_employee(r.get("snapshot")) == employee_id]
    return [SubmissionStatus(**_submission_status(r)) for r in rows]


@router.post(
    "/submissions/{request_id}/retry",
    response_model=SubmissionStatus,
    summary="重试失败的异步提交任务（报销端）",
    description="""
把 `failed` 的任务复位为 `pending` 并重新投递执行（报销人修正输入后可重试，无需整单重传；
broker 不可达时降级同步直跑）。

- 仅 `failed` 可重试：`pending/processing` 是进行中，`succeeded` 已完成 → 400
- 重试成功后前端继续轮询（pending → succeeded），单据在 `succeeded` 后查询详情
""",
)
def retry_failed_submission(request: Request, request_id: str):
    """失败任务重试入口：仅 failed → pending 重新投递"""
    container = _container(request)
    try:
        result = retry_submission(container, request_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    return SubmissionStatus(**result)


@router.post(
    "/requests/{request_id}/decide",
    response_model=RequestDetail,
    summary="审批决策（审核端 / 领导端）",
    description="""
恢复挂起的人工审批并提交决策。**允许的动作随当前步骤自动校验**（防止越权；判定读图实时状态，
双写落后不会放行错步/离场角色）：

| 当前步骤 | 可用动作 | 结果 |
|---|---|---|
| `review`（审核人员复核） | `approve` | 批准 → 挂起到领导决策（并给领导发邮件） |
| `review`（审核人员复核） | `return` | 退回 → 报销单 `returned`，通知报销人修改重提 |
| `leader_decision`（领导决策） | `approve` | 最终批准 → 单据 `approved`，通知财务出纳付款 |
| `leader_decision`（领导决策） | `void` | 作废 → 单据 `voided`，通知全部审批链角色 |

**幂等**：单据已终态且本次决策与已记录完全一致（双击 / 客户端重试）→ 直接返回当前状态，不二次推进、不重发通知。

**入参**：`action`（approve / return / void）、`comment`（意见，退回/作废时填原因）、`actor`（决策人工号，Demo 直接传，真实系统从登录态获取）。

**返回**：决策后的最新状态（步骤推进 / 终态 + 通知留痕）。
""",
)
def decide_request(
    request: Request,
    request_id: str,
    body: DecideRequest,
):
    """审批决策：审核人员 approve/return；领导 approve/void（实时态校验收敛在 service.decide）"""
    # 业务：路由层只做"存在性 + 错误映射"（#B）——允许动作/幂等闸/实时步判定全在 decide 内一处完成，
    #       避免路由按落后的行拦截/放行架空实时态校验；存在性用实时态（checkpoint 在、行缺失也算存在）
    container = _container(request)
    if _live_state(container, request_id) is None:
        raise HTTPException(status_code=404, detail="报销单不存在")
    # 业务：decide 内决策人必须与审批链当前步骤角色一致（越权 → 403）；
    #       批准软闸门未通过项须填意见（#88，抛 ValueError → 400，非 500）
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


def _allowed_original_keys(state: dict) -> set[str]:
    """本单允许查看原件的 object_key 集（#93 对象存储取用接线：只放行属于本单的票）

    来源：state 的 tickets[]（被接受并入审）+ rejected[]（被拒不入池）+ invoice_input
    （老单票兼容镜像）里每张票 invoice_input.object_key——uploader 与 file_path 同 key 落对象存储。
    """
    keys: set[str] = set()
    for holder in [state.get("invoice_input")]:
        if holder and holder.get("object_key"):
            keys.add(holder["object_key"])
    for arr in ["tickets", "rejected"]:
        for item in state.get(arr) or []:
            inv = item.get("invoice_input") if isinstance(item, dict) else None
            if inv and inv.get("object_key"):
                keys.add(inv["object_key"])
    return keys


@router.get(
    "/requests/{request_id}/originals/{object_key}",
    summary="查看/下载发票原件（对象存储取用，#93）",
    description="""
按票据的 `object_key` 取发票原件字节返回（源文件权威副本在对象存储，本地临时缓存不可依赖）。

**权限边界**：只放行**属于本单**的票（tickets/rejected 内的 object_key），伪造 key → 404；
图片/PDF 按媒体类型内联预览（浏览器新标签直接打开），其余类型浏览器按下载处理（保留原文件名）。

报销端查看自己的原件、审核端/领导端复核 Agent 结论时核对票面用。
""",
)
def get_original(request: Request, request_id: str, object_key: str):
    """发票原件字节（从对象存储取，不做本地缓存依赖；key 属本单才放行）"""
    container = _container(request)
    state = container.storage.get_request(request_id)
    if state is None:
        raise HTTPException(status_code=404, detail="报销单不存在")
    if object_key not in _allowed_original_keys(state):
        raise HTTPException(status_code=404, detail="该原件不属于本报销单")
    try:
        content = container.object_storage.get(object_key)
    except Exception as exc:  # 对象不存在/MinIO 不可达 → 按不存在处理（不泄漏存在性细节）
        raise HTTPException(status_code=404, detail=f"发票原件不存在或已被清理: {exc}")
    # 媒体类型按扩展名；object_key = {时间戳}_{原文件名}（uploader），去掉时间戳前缀还原展示名
    name = object_key.split("_", 1)[-1]
    ext = Path(object_key).suffix.lower()
    media_type = _CONTENT_TYPES.get(ext, "application/octet-stream")
    return Response(
        content=content,
        media_type=media_type,
        headers={
            # RFC 5987：中文原文件名以 filename* 携带，浏览器下载保留原名
            "Content-Disposition": f"inline; filename*=UTF-8''{quote(name)}; filename=\"{name}\""
        },
    )


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
