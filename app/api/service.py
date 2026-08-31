# app/api/service.py —— 接口编排（调用总控图 + 持久化）
# 业务：API 与图之间的薄胶水层：启动图执行 / 恢复 HITL / 落库 / 组装视图
from datetime import datetime

from langgraph.types import Command

from app.container import Container


def _clean_state(state: dict) -> dict:
    """剔除运行期字段（__interrupt__ 等不可 JSON 序列化），只留业务状态"""
    # 作用：__ 前缀为 langgraph 运行期内部键，不能落库/返回
    return {k: v for k, v in state.items() if not k.startswith("__")}


def _run(graph, payload, config: dict) -> dict:
    """执行图并取最终 state（values 模式；含 __interrupt__ 键表示挂起）"""
    # 作用：stream 能稳定拿到"中断点"或"终态"的完整状态
    values = list(graph.stream(payload, config, stream_mode="values"))
    return values[-1]


def start_reimbursement(container: Container, invoice_input: dict, request_id: str) -> dict:
    """提交报销：启动总控图 → 返回首个挂起点（等待审核人员复核）"""
    initial = {
        "request_id": request_id,
        "invoice_input": invoice_input,
        "process_status": "in_review",
    }
    config = {"configurable": {"thread_id": request_id}}
    result = _run(container.graph, initial, config)
    container.storage.upsert_request(
        request_id,
        _clean_state(result),
        result.get("process_status", "in_review"),
        result.get("current_step", ""),
    )
    return result


def decide(container: Container, request_id: str, action: str, comment: str, actor: str) -> dict:
    """审批决策：先校验决策人权限，再以 Command(resume=...) 恢复 HITL"""
    # 业务：越权是报销系统第一安全风险——当前步骤必须由审批链对应角色本人操作，
    #       否则任何人可伪造审批（如 4001 跳过 2001 直接批准）。不通过抛 PermissionError。
    _authorize(container, request_id, actor)
    resume = {"action": action, "comment": comment, "actor": actor}
    config = {"configurable": {"thread_id": request_id}}
    result = _run(container.graph, Command(resume=resume), config)
    container.storage.upsert_request(
        request_id,
        _clean_state(result),
        result.get("process_status", "in_review"),
        result.get("current_step", ""),
    )
    return result


def pay(container: Container, request_id: str, comment: str, actor: str) -> dict:
    """出纳打款：审批通过(approved)后由财务出纳确认打款 → 单据 paid"""
    # 业务：审批≠支付——批准是审批链终点，打款是财务域动作（出纳身份按制度角色"财务"解析）
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
    records.append({
        "role": "财务出纳",
        "decision": "pay",
        "actor": actor,
        "comment": comment,
        "time": now,
    })
    new_state = {
        **state,
        "process_status": "paid",
        "current_step": "done",
        "approval_records": records,
        "payment": {"actor": actor, "comment": comment, "time": now},
    }
    container.storage.upsert_request(request_id, new_state, "paid", "done")
    # 业务：打款完成后通知报销人到账
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
    return {
        "request_id": clean.get("request_id"),
        "status": clean.get("process_status", "in_review"),
        "current_step": clean.get("current_step", ""),
        "business_type": clean.get("business_type", ""),
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
        "paused": "__interrupt__" in state,
        "messages": container.storage.list_messages(clean["request_id"]),
        "emails": container.storage.list_emails(clean["request_id"]),
    }
