# app/tasks/sla_task.py —— 周期任务：审批 SLA 催办/升级（#62 人工审批时限兜底）
# 业务：单据停在"待某角色人工审批"（current_step=review/leader_decision）超制度时限无人处理时，
#       系统不能无限等——按 policy/<direction>.yaml 的 approval_sla_hours / approval_escalate_hours
#       周期扫描：超过 SLA 时限 → 催办当前审批人（站内消息留痕）；超过升级时限仍无动作 → 升级给监督角色。
#       **审批权不自动移交**（越权风险，审批仍须本人操作），升级=监督性催办通知（口径见 travel.yaml §SLA 注释）。
# 周期：celery beat 触发（schedule 见 celery_app.py beat_schedule）；本地 dev 用 -B/独立 beat（同 reclaim）。
# 纯逻辑在 sla_sweep_once()：不依赖 celery，测试可直调；celery task 只是周期入口。
from datetime import datetime

from app.celery_app import celery_app
from app.core.logging import get_logger, log_error, log_info

logger = get_logger("tasks.sla")

# 审批步骤文案（催办/升级通知里给人看的步骤名）
_STEP_LABELS = {"review": "审核复核", "leader_decision": "领导决策"}


def _parse_ts(value: str) -> datetime:
    """解析 ISO 时间戳；解析失败按最旧处理（宁催办不错放）"""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min


def _direction(state: dict) -> str:
    """单据业务方向（SLA 时限取自对应 policy/<direction>.yaml；行/图字段都可能是来源）"""
    for source in (state.get("business_type"),
                   (state.get("invoice_input") or {}).get("direction"),
                   ((state.get("invoice_inputs") or [{}])[0]).get("direction")):
        if source:
            return source
    return "travel"  # 兜底：历史单无方向字段时按差旅口径


def _amount(state: dict) -> float:
    """报销金额口径：#A 多票批 = Σ 被接受票面（total_amount）；旧单回退单票 invoice_data.amount"""
    total = state.get("total_amount")
    if total is not None:
        return float(total)
    return float((state.get("invoice_data") or {}).get("amount") or 0)


def _approver(state: dict) -> dict | None:
    """当前步骤的责任审批人：review=链首（直属上级），leader_decision=链尾（总经理/最终决策人）"""
    chain = state.get("approval_chain") or []
    if not chain:
        return None
    return chain[0] if state.get("current_step") == "review" else chain[-1]


def _escale_to(chain: list[dict], approver: dict) -> str:
    """升级对象角色：审批人非链尾 → 升级给链尾（更高一级）；审批人已是链尾/单级 → 升级给财务监督"""
    if chain and chain[-1].get("id") != approver.get("id"):
        return chain[-1].get("role", "总经理")
    return "财务"


@celery_app.task(name="flowinvoice.sla_sweep", ignore_result=True)
def sla_sweep():
    """Celery beat 周期入口：扫一次挂起单据，超时限催办/升级（#62）"""
    from app.tasks.submit_task import get_container

    container = get_container()
    try:
        stats = sla_sweep_once(container)
        if stats["reminded"] or stats["escalated"]:
            log_info(logger, "审批 SLA 周期催办/升级", reminded=stats["reminded"],
                     escalated=stats["escalated"], scanned=stats["scanned"])
    except Exception as exc:
        log_error(logger, "审批 SLA 周期任务失败", error=str(exc))


def sla_sweep_once(container) -> dict:
    """纯函数：扫一次挂起单据，超时限催办/升级。返回统计 {"scanned","reminded","escalated"}"""
    # 业务：以 _live_state（图 checkpoint 实时态）判"是否真挂起"——行落后于图（落库前崩溃）时
    #       不按旧行误催已推进的单据；SLA 锚点/标记（paused_at/sla）只存在行快照（不经图通道），
    #       故读写都落在行上、催办不触碰图。
    from app.api import service as svc  # 惰性导入：避免启动装配顺序耦合（同 reclaim 范式）

    stats = {"scanned": 0, "reminded": 0, "escalated": 0}
    for row in container.storage.list_requests(status="in_review"):
        rid = row["request_id"]
        live = svc._live_state(container, rid)
        if not live:
            continue
        step = live.get("current_step", "")
        if live.get("process_status") in svc.TERMINAL_STATUSES or step not in ("review", "leader_decision"):
            continue  # 已终态 / 图在途：不催
        row_state = container.storage.get_request(rid) or live  # 行持有 paused_at/sla 锚点
        try:
            policy = container.policies.load(_direction(row_state))
        except FileNotFoundError:
            continue  # 该方向无政策 yaml：无 SLA 制度可循，跳过
        sla_h = policy.get("approval_sla_hours") or 0
        esc_h = policy.get("approval_escalate_hours") or 0
        if sla_h <= 0:
            continue  # 0 = 制度关闭 SLA 兜底（travel.yaml 注释口径）
        # 锚点 = 挂起起始（每级审批由 _stamp_pause 重记 paused_at）；无 paused_at（老单）回退行 updated_at
        waiting = _parse_ts(row_state.get("paused_at") or row.get("updated_at") or "")
        elapsed_h = (datetime.now() - waiting).total_seconds() / 3600.0
        marker = dict(row_state.get("sla") or {})
        approver = _approver(live)
        if not approver:
            continue
        # 升级优先：达升级时限即升（已升级过 → 一次性，不再重复打扰，等单据动后自然收敛）
        if esc_h > 0 and elapsed_h >= esc_h and not marker.get("escalated_at"):
            if _notify_sla(container, rid, row_state, marker, approver, step,
                           escalated=True, sla_h=sla_h, esc_h=esc_h, elapsed_h=elapsed_h):
                stats["escalated"] += 1
        elif elapsed_h >= sla_h and not marker.get("reminded_at"):
            if _notify_sla(container, rid, row_state, marker, approver, step,
                           escalated=False, sla_h=sla_h, esc_h=esc_h, elapsed_h=elapsed_h):
                stats["reminded"] += 1
        stats["scanned"] += 1
    return stats


def _notify_sla(container, rid: str, state: dict, marker: dict, approver: dict, step: str, *,
                escalated: bool, sla_h: int, esc_h: int, elapsed_h: float) -> bool:
    """发一条催办/升级通知并落 SLA 标记；返回是否成功落标记（防并发覆盖失败则不算）"""
    # 业务：先发通知（人要先看见），再落标记；落前重读行做 CAS 式校验——
    #       beat 扫与 decide 双写并发时，若单据已被推进/重挂（步骤/状态/锚点变化），不再用旧态覆盖新行。
    #       通知可能已发、标记未落 → 下轮读新状态自会收敛（单据动了就不符合催办条件），不会反复打扰。
    employee_id = (state.get("invoice_input") or {}).get("employee_id", "")
    step_label = _STEP_LABELS.get(step, step)
    now_iso = datetime.now().isoformat(timespec="seconds")
    amount = _amount(state)
    if escalated:
        esc_role = _escale_to(state.get("approval_chain") or [], approver)
        title = f"报销单 {rid} 审批超时（SLA 升级）"
        content = (
            f"单据挂起已超 {esc_h} 小时，催办后仍无人处理，升级给 {esc_role} 监督催办。\n"
            f"原待办：{step_label} · {approver.get('role', '')}({approver.get('id', '')})\n"
            f"报销人 {employee_id}，金额 ¥{amount:,.2f}，请协助推进。"
        )
        to_role = esc_role
    else:
        title = f"报销单 {rid} 审批超时（SLA 催办）"
        content = (
            f"单据挂起已超 {sla_h} 小时仍待审批，系统自动催办。\n"
            f"待办：{step_label} · {approver.get('role', '')}({approver.get('id', '')})\n"
            f"报销人 {employee_id}，金额 ¥{amount:,.2f}；仍超时未处理将升级监督。"
        )
        to_role = approver.get("role", "")
    if not to_role:
        return False
    container.notify_provider.send(rid, to_role, title, content)
    # CAS 写回：仅当行仍是同一挂起（同 status/step/paused_at）才落标记，避免覆盖已被 decide 推进的新行
    fresh = container.storage.get_request(rid)
    if fresh is None:
        return False
    if (fresh.get("process_status") != state.get("process_status")
            or fresh.get("current_step") != state.get("current_step")
            or fresh.get("paused_at") != state.get("paused_at")):
        return False
    marker["escalated_at" if escalated else "reminded_at"] = now_iso
    fresh["sla"] = marker
    container.storage.upsert_request(rid, fresh, fresh.get("process_status", "in_review"),
                                     fresh.get("current_step", ""))
    log_info(logger, "审批 SLA " + ("升级" if escalated else "催办"), request_id=rid,
             step=step, actor=approver.get("id", ""), elapsed_h=round(elapsed_h, 1))
    return True
