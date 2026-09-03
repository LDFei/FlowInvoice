# app/api/service.py —— 接口编排（调用总控图 + 持久化）
# 业务：API 与图之间的薄胶水层：启动图执行 / 恢复 HITL / 落库 / 组装视图
import threading
from datetime import datetime

from langgraph.types import Command

from app.container import Container
from app.core.config import ASYNC_ENABLED
from app.core.logging import get_logger, log_error, log_info, log_warning, set_log_context
from app.core.status import SubmissionStatus

logger = get_logger("api.service")

# 作用：审批/打款是"读-改-写"临界区，进程内锁防并发双写（Demo 单进程；多实例需分布式锁）
_lock = threading.Lock()


def _clean_state(state: dict) -> dict:
    """剔除运行期字段（__interrupt__ 等不可 JSON 序列化），只留业务状态"""
    # 作用：__ 前缀为 langgraph 运行期内部键，不能落库/返回
    return {k: v for k, v in state.items() if not k.startswith("__")}


def _has_failed_soft_checks(state: dict) -> bool:
    """软闸门复核衔接（#88）：存在未通过的合规检查项（超预算/席别越级等，passed=False）"""
    # 业务：合规检查是软闸门——不阻断流程、由人工复核裁决；但"批准未通过项"必须留批注，
    #       否则合规标记形同虚设、审计无据（企业条例可裁量，裁量要可追溯）
    return any(c.get("passed") is False for c in (state.get("compliance_checks") or []))


def _release_invoice_on_terminal(container: Container, request_id: str, state: dict) -> None:
    """发票池释放：退回/作废释放票号占用（报销人可修改重提）；打款/审批中保持占用（防重复报销）"""
    # 业务：图执行到终态后统一收口——非支付终态释放占用，否则同一发票无法重提；已打款保持 active 永久拦截
    if state.get("process_status") in ("returned", "voided"):
        container.storage.release_invoice(request_id)


def _reserve_advance_on_terminal(container: Container, state: dict) -> None:
    """事前申请预算占用：报销到达 approved/paid 终态 → 按票面金额占一笔（advance_reservations 台账）"""
    # 业务：预算池核销（docs/02 §12）——approved 即占用（终态不可退回）、paid 兜底幂等
    #       （request_id 唯一，approve+paid 双入口不重复累计）；returned/voided 不占用（报销未成功）。
    #       申请状态不翻 used——一次出差多张票共享同一申请逐笔累计占用，直到区间/有效期自然结束。
    #       已知限制：审批中并发提交的"在途"占用不入本单 state 快照（reserved_amount 为提交时点值）。
    if state.get("process_status") not in ("approved", "paid"):
        return
    app = state.get("advance_application") or {}
    invoice = state.get("invoice_data") or {}
    amount = invoice.get("amount")
    request_id = state.get("request_id")
    if app.get("app_id") and request_id and amount:
        total = container.advances.reserve(app["app_id"], request_id, float(amount))
        log_info(logger, "事前申请预算占用", app_id=app["app_id"], request_id=request_id,
                 amount=float(amount), total=total)


def _run(graph, payload, config: dict) -> dict:
    """执行图并取最终 state（values 模式；含 __interrupt__ 键表示挂起）"""
    # 作用：stream 能稳定拿到"中断点"或"终态"的完整状态
    values = list(graph.stream(payload, config, stream_mode="values"))
    return values[-1]


def run_submit_pipeline(container: Container, invoice_input: dict, request_id: str) -> dict:
    """执行提交管线：启动总控图 → 落库 → 发票池终态释放（同步直跑 与 worker 任务共用）"""
    # 作用：把"图执行+落库+释放"从 HTTP 请求线程抽出来（#52 D4）——同步模式 start_reimbursement
    #       直接调用；异步模式由 Celery worker 用 submissions.snapshot 重放。
    #       把 request_id 注入关联上下文，本条链路的图节点/适配器日志自动携带，可整条串查
    set_log_context(request_id=request_id)
    log_info(logger, "报销提交：启动图执行", business_type=invoice_input.get("direction", ""))
    initial = {
        "request_id": request_id,
        "invoice_input": invoice_input,
        "process_status": "in_review",
    }
    config = {"configurable": {"thread_id": request_id}}
    try:
        result = _run(container.graph, initial, config)
    except Exception as exc:
        # 业务：图执行中途异常（非业务退回）——verify 节点可能已把票号入池（active），异常路径若不清理
        #       该票号残留占用 → 报销人被误判"重复提交"无法重提。异常同样要释放（幂等：无 active 则 no-op）
        log_error(logger, "报销提交异常，清理发票池占用", error=str(exc))
        container.storage.release_invoice(request_id)
        raise
    container.storage.upsert_request(
        request_id,
        _clean_state(result),
        result.get("process_status", "in_review"),
        result.get("current_step", ""),
    )
    _release_invoice_on_terminal(container, request_id, result)
    _reserve_advance_on_terminal(container, result)
    # 作用：提交结果落日志——退回（业务失败）与挂起（成功待审）区分记录，失败原因已由退回节点分类
    if result.get("process_status") == "returned":
        reason = result.get("return_reason", {})
        log_warning(logger, "报销提交失败（业务退回）", category=reason.get("category", ""), message=reason.get("message", ""))
    else:
        log_info(logger, "报销提交成功，进入审批", current_step=result.get("current_step", ""))
    return result


def start_reimbursement(container: Container, invoice_input: dict, request_id: str) -> dict:
    """提交报销：同步模式直跑；异步模式落任务行入队，返回 pending 受理凭证"""
    # 作用：异步模式把重负载（OCR/LLM/RAG/DB）搬进 Celery worker，HTTP 请求立即返回；
    #       broker 不可达 → 降级同步直跑（外部依赖缺失降级，项目一贯哲学），API 不因 Redis 宕机不可用
    if not ASYNC_ENABLED:
        return run_submit_pipeline(container, invoice_input, request_id)
    # 先落任务行（快照=原始输入，worker 失败可重放），再入队——worker 领单依赖该行已存在
    container.storage.create_submission(request_id, snapshot=invoice_input, status=SubmissionStatus.PENDING)
    try:
        from app.tasks.submit_task import process_submission

        process_submission.delay(request_id)
    except Exception as exc:  # celery 未装 / broker 不可达 → 降级同步直跑，并把任务行对齐为实际结果
        log_warning(logger, "异步入队失败（broker 不可达），降级同步直跑", error=str(exc))
        try:
            run_submit_pipeline(container, invoice_input, request_id)
        except Exception as pipe_exc:
            container.storage.update_submission(
                request_id,
                status=SubmissionStatus.FAILED,
                error={"type": type(pipe_exc).__name__, "message": str(pipe_exc)},
                attempts=1,
            )
            raise
        container.storage.update_submission(request_id, status=SubmissionStatus.SUCCEEDED)
    return {"request_id": request_id, "status": SubmissionStatus.PENDING}


def decide(container: Container, request_id: str, action: str, comment: str, actor: str) -> dict:
    """审批决策：校验决策人权限 → 以 Command(resume=...) 恢复 HITL → 落库"""
    # 业务：越权是报销系统第一安全风险——当前步骤必须由审批链对应角色本人操作，
    #       否则任何人可伪造审批（如 4001 跳过 2001 直接批准）。不通过抛 PermissionError。
    # 作用：权限校验与 resume 同一把锁内——并发双击时第二次读到的是第一次已推进的状态，
    #       （如 review→leader_decision 后 2001 再批）→ 权限校验直接拒绝，杜绝 TOCTOU 双跑。
    set_log_context(request_id=request_id)
    resume = {"action": action, "comment": comment, "actor": actor}
    config = {"configurable": {"thread_id": request_id}}
    with _lock:
        # 业务：硬闸门已把硬性问题挡在退回；仍走到人工复核的未通过项属可裁量的软闸门——
        #       批准须填复核意见（特批理由），否则拒绝（复核≠橡皮图章，意见留痕供审计追溯）
        _authorize(container, request_id, actor)
        if action == "approve" and _has_failed_soft_checks(container.storage.get_request(request_id) or {}):
            if not (comment or "").strip():
                raise ValueError("存在未通过的合规检查项（如超预算/席别超标准），批准须填写复核意见（特批理由）")
        result = _run(container.graph, Command(resume=resume), config)
        container.storage.upsert_request(
            request_id,
            _clean_state(result),
            result.get("process_status", "in_review"),
            result.get("current_step", ""),
        )
    _release_invoice_on_terminal(container, request_id, result)
    _reserve_advance_on_terminal(container, result)
    log_info(logger, "审批决策完成", action=action, actor=actor, status=result.get("process_status", ""))
    return result


def pay(container: Container, request_id: str, comment: str, actor: str) -> dict:
    """出纳打款：审批通过(approved)后由财务出纳确认打款 → 单据 paid"""
    # 业务：审批≠支付——批准是审批链终点，打款是财务域动作（出纳身份按制度角色"财务"解析）
    set_log_context(request_id=request_id)
    with _lock:
        state = container.storage.get_request(request_id)
        if state is None:
            raise ValueError(f"报销单不存在: {request_id}")
        if state.get("process_status") != "approved":
            raise ValueError(f"仅已批准单据可打款，当前状态({state.get('process_status')})")
        finance = container.users.get_approver("财务")
        if actor != finance["id"]:
            raise PermissionError(f"打款仅限出纳 {finance['id']} 执行，实际 {actor or '未指定'}")
        now = datetime.now().isoformat(timespec="seconds")
        records = list(state.get("approval_records", []))
        record = {
            "role": "财务出纳",
            "decision": "pay",
            "actor": actor,
            "comment": comment,
            "time": now,
        }
        records.append(record)
        # 拆表落库（docs/06 §5：approval_records 表为权威）
        container.storage.add_approval_record(request_id, record)
        new_state = {
            **state,
            "process_status": "paid",
            "current_step": "done",
            "approval_records": records,
            "payment": {"actor": actor, "comment": comment, "time": now},
        }
        container.storage.upsert_request(request_id, new_state, "paid", "done")
    # 业务：打款完成后通知报销人到账；事前申请在 approved 已占用，paid 幂等兜底（不重复累计）
    _reserve_advance_on_terminal(container, new_state)
    container.notify_tool.notify_paid(request_id, new_state)
    return new_state


def _authorize(container: Container, request_id: str, actor: str) -> None:
    """权限校验：按审批链 + 当前步骤确定唯一审批人，actor 必须与其匹配"""
    # 业务：review 步=审批链第一级（直属上级）；leader_decision 步=审批链末级（总经理/最终决策人）
    state = container.storage.get_request(request_id)
    if state is None:
        raise ValueError(f"报销单不存在: {request_id}")
    chain = state.get("approval_chain") or []
    step = state.get("current_step", "")
    if step not in ("review", "leader_decision"):
        raise PermissionError(f"当前步骤({step})不可审批")
    if not chain:
        raise PermissionError("审批链信息缺失，无法校验权限")
    expected = chain[0] if step == "review" else chain[-1]
    if actor != expected["id"]:
        raise PermissionError(
            f"权限校验失败：当前步骤({step})应由 {expected['role']}({expected['id']}) 决策，"
            f"实际提交人 {actor or '未指定'}"
        )


def request_view(container: Container, state: dict) -> dict:
    """组装给前端/调用方的视图（含通知与邮件留痕）"""
    # 业务：审核端/报销端关心的字段；paused 标记当前是否处于人工挂起
    clean = _clean_state(state)
    # 作用：paused 由 process_status + current_step 推导，不再依赖 __interrupt__ 运行期键——
    #       落库 state 经 _clean_state 剥离 __ 键，GET 详情拿不到它（此前恒 False 的 bug）。
    #       实时图状态含 __interrupt__ 时以它为准（等价于挂起）
    terminal = clean.get("process_status") in ("approved", "returned", "voided", "paid")
    paused = ("__interrupt__" in state) or (
        not terminal and clean.get("current_step") in ("review", "leader_decision")
    )
    return {
        "request_id": clean.get("request_id"),
        "status": clean.get("process_status", "in_review"),
        "current_step": clean.get("current_step", ""),
        "business_type": clean.get("business_type", ""),
        # 作用：#89 退回重提留痕——本次报销若由某报销单退回/作废后重提，记录其原单号（无则不填）
        "parent_request_id": (clean.get("invoice_input") or {}).get("parent_request_id", "") or None,
        "summary": clean.get("summary", ""),
        "invoice_data": clean.get("invoice_data"),
        "verification": clean.get("verification"),
        "advance_application": clean.get("advance_application"),
        "compliance_checks": clean.get("compliance_checks"),
        "policy_basis": clean.get("policy_basis"),
        "approval_chain": clean.get("approval_chain"),
        "return_reason": clean.get("return_reason"),
        "approval_records": clean.get("approval_records"),
        "decision": clean.get("decision"),
        "payment": clean.get("payment"),
        "paused": paused,
        "messages": container.storage.list_messages(clean["request_id"]),
        "emails": container.storage.list_emails(clean["request_id"]),
    }
