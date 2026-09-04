# app/tasks/reclaim_task.py —— 周期自愈任务：回收卡死的异步提交 + 孤儿 checkpoint 对账（B 阶段接入）
# 业务：worker 是独立进程，崩溃后"processing 却无 worker 在跑"的任务不会被任何人续上——
#       启动时 API 的 recover_stuck 只在重启那刻兜底，运行期要靠本任务周期扫：
#       超过阈值仍 processing → 判定结果是否已落地：未落地才复位 pending 并重新投递（worker 重跑），
#       实现运行期自愈。由 celery beat 触发（schedule 见 celery_app.py beat_schedule）；本地 dev 用 -B 内嵌 beat。
from datetime import datetime

from app.celery_app import celery_app
from app.core.config import STUCK_AFTER_SECONDS
from app.core.logging import get_logger, log_error, log_info, log_warning
from app.core.status import SubmissionStatus

logger = get_logger("tasks.reclaim")


def _parse_ts(value: str) -> datetime:
    """解析存储返回的 ISO 时间戳（两种实现同一格式）；解析失败按最旧处理（宁回收不错放）"""
    try:
        return datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return datetime.min


@celery_app.task(name="flowinvoice.reclaim_stuck", ignore_result=True)
def reclaim_stuck():
    """Celery beat 周期任务入口：回收粘滞提交 + 孤儿 checkpoint 对账（B 阶段）"""
    from app.tasks.submit_task import get_container

    container = get_container()
    try:
        reclaimed = recover_stuck_submissions(container)
        if reclaimed:
            log_info(logger, "周期回收：粘滞提交已收口", count=reclaimed)
    except Exception as exc:
        log_error(logger, "周期回收失败", error=str(exc))
    try:
        reconciled = reconcile_orphan_checkpoints(container)
        if reconciled:
            log_info(logger, "周期对账：孤儿 checkpoint 已物化为单据", count=reconciled)
    except Exception as exc:
        log_error(logger, "周期对账失败", error=str(exc))


def recover_stuck_submissions(container, *, force=False) -> int:
    """粘滞提交统一收口（#94 崩溃窗口语义，替换盲"复位+重投"；供 beat 周期 + 启动 force 复用）

    扫描 **processing + pending**（补 pending：投递丢失/broker 不可达时复位后未投的僵尸行——
    停 pending 永不 claim，盲复位正制造这类僵尸）逐行判定（默认超 STUCK_AFTER_SECONDS；force=启动
    那刻所有行均来自已死 worker → 忽略阈值全量判定）：

    - **结果已落地**（requests 行已写且停在 HITL/终态，即 worker 在 update_submission(SUCCEEDED) **前**
      崩溃）：补 succeeded，**不重跑**——重跑 = 重发审核通知 + 发票重入池造成假查重覆盖已审单据。
    - **真中途崩溃**（行缺失/停在在途）：先 release_invoice 清首轮已入池的票（防重跑被假查重拦截，
      幂等 no-op）→ requeue_submission(processing/pending→pending) + 重新投递让 worker 领单重跑；
      broker 不可达 → 捕获，任务行已复位 pending，下轮周期再投（不丢）。
    """
    from app.api.service import submit_outcome_materialized
    from app.tasks.submit_task import process_submission

    rows = container.storage.list_submissions(status="processing")
    rows += container.storage.list_submissions(status="pending")
    now = datetime.now()
    fixed: list[str] = []
    for row in rows:
        # 作用：updated_at=领取时间——worker 执行期间不再心跳，只有拿到超阈值才算"卡死"；
        #       正常长任务（如多票批量 OCR）远低于阈值，不会被误重置成重复跑
        updated_at = _parse_ts(row.get("updated_at") or "")
        if not force and (now - updated_at).total_seconds() < STUCK_AFTER_SECONDS:
            continue
        request_id = row["request_id"]
        if submit_outcome_materialized(container, request_id):
            # 首轮已完整跑完（图挂起/终态、行已写），只差 succeeded 未落 → 补终态，绝不重跑
            container.storage.update_submission(request_id, status=SubmissionStatus.SUCCEEDED)
            log_info(logger, "回收：提交结果已落地，补终态不重跑", request_id=request_id)
        else:
            # 首轮真中途崩溃：清残留入池票（防重跑假查重）→ 复位 + 重新投递（worker 领单重跑）
            container.storage.release_invoice(request_id)
            container.storage.requeue_submission(request_id)   # processing|pending → pending
            try:
                process_submission.delay(request_id)           # 重新投递，worker 靠 pending 领取重跑
            except Exception as exc:  # broker 又不可达：任务行已复位 pending，下次周期再投（不丢）
                log_warning(logger, "回收任务重新投递失败，下轮再试", request_id=request_id, error=str(exc))
        fixed.append(request_id)
    return len(fixed)


def reconcile_orphan_checkpoints(container) -> int:
    """孤儿 checkpoint → 物化 requests 行（B 阶段启动对账；reclaim 周期内同步兜底）"""
    from app.api import service as svc  # 惰性导入：避免启动装配顺序耦合

    fn = getattr(svc, "materialize_orphan_checkpoints", None)
    return fn(container) if fn is not None else 0
