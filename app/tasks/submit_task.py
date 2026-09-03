# app/tasks/submit_task.py —— Celery worker 任务：领单 → 跑提交管线 → 落库
# 业务：#52 把"提交→处理→挂起"从 HTTP 请求线程搬进 worker；API 只负责入队，worker 消费（docs/06 §2）
# 注意：worker 是独立进程，无法复用 API 进程的 Container —— 各自 build_container()，
#       checkpointer 指向同一持久化库（PostgresSaver/SqliteSaver）→ decide 跨进程恢复 HITL
from app.api.service import run_submit_pipeline
from app.celery_app import celery_app
from app.container import build_container
from app.core.logging import get_logger, log_error, log_info, log_warning, set_log_context
from app.core.status import SubmissionStatus

logger = get_logger("tasks.submit_task")

_container = None


def get_container():
    """worker 进程内惰性构建容器（API 进程不可复用）；测试可注入见 set_container()"""
    # 作用：复用 build_container() 工厂（含 ids.seed_from_existing 单号续接）；一次构建进程复用
    global _container
    if _container is None:
        _container = build_container()
    return _container


def set_container(container) -> None:
    """测试注入：让 eager 模式任务使用 fixture 容器（避免默认容器污染全局 data/）"""
    global _container
    _container = container


@celery_app.task(
    bind=True,
    name="flowinvoice.process_submission",
    retry_backoff=True,      # 失败退避 1,2,4,8... 秒
    retry_backoff_max=60,
    retry_jitter=True,
    max_retries=3,
)
def process_submission(self, request_id: str):
    """领取并执行一次提交管线（幂等：claim 失败直接丢弃，防重复投递双跑）"""
    container = get_container()
    # 原子领取：pending → processing；失败 = 已被领/已终态 → 直接返回（重复投递幂等）。
    # 重试投递（self.request.retries>0）时我们仍持有 processing 行（领单改的状态）——
    # 若重跑 claim 会因状态已是 processing 返回 False 被丢弃，故重投跳过 claim 直接续跑
    if self.request.retries == 0 and not container.storage.claim_submission(request_id):
        log_info(logger, "任务已被处理，丢弃重复投递", request_id=request_id)
        return
    set_log_context(request_id=request_id)
    sub = container.storage.get_submission(request_id)
    if sub is None:  # 任务行被外部清理（异常状态），无处可跑
        log_error(logger, "任务行不存在，终止", request_id=request_id)
        return
    log_info(logger, "任务领取成功，开始执行提交管线", request_id=request_id)
    try:
        run_submit_pipeline(container, sub["snapshot"], request_id)
    except Exception as exc:
        # 重试计数落库（前端展示/可观测）；达到上限 → 置 failed 并抛出（Celery 记任务失败）
        attempts = (sub.get("attempts") or 0) + 1
        if attempts >= self.max_retries:
            container.storage.update_submission(
                request_id,
                status=SubmissionStatus.FAILED,
                error={"type": type(exc).__name__, "message": str(exc)},
                attempts=attempts,
            )
            log_error(logger, "异步任务重试耗尽，最终失败", request_id=request_id, error=str(exc))
            raise
        container.storage.update_submission(request_id, attempts=attempts)
        log_warning(logger, "异步任务失败，退避重试", request_id=request_id, attempts=attempts, error=str(exc))
        raise self.retry(exc=exc)  # retry_backoff 自动退避
    # 成功：succeeded（终态），任务行完成
    container.storage.update_submission(request_id, status=SubmissionStatus.SUCCEEDED)
    log_info(logger, "异步任务处理成功", request_id=request_id)
