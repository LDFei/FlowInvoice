# app/celery_app.py —— Celery 实例（broker=Redis；任务注册在 app/tasks/）
# 业务：#52 异步执行层：提交→处理→挂起 进 worker，API 提交即返回 pending（docs/06 §2）
# 启动：celery -A app.celery_app worker -P solo（Windows 无 prefork；Linux 可省 -P solo）
# 回收：beat 周期触发 flowinvoice.reclaim_stuck（见 app/tasks/reclaim_task.py）——
#       周期任务需独立 beat 进程。POSIX dev 可用 `-B` 内嵌（worker 同进程跑）；Windows 不支持
#       `-B`，须另起 `python -m celery -A app.celery_app beat`（scripts/dev_async.ps1 已一并拉起）
from celery import Celery
from celery.schedules import schedule

from app.core.config import REDIS_DSN

celery_app = Celery(
    "flowinvoice",
    broker=REDIS_DSN,
    include=[
        "app.tasks.submit_task",
        "app.tasks.reclaim_task",  # 周期回收（粘滞提交/孤儿 checkpoint），beat 触发
        "app.tasks.sla_task",      # 周期审批 SLA 催办/升级（#62），beat 触发
    ],
)

celery_app.conf.update(
    task_track_started=True,              # worker 开始执行即标记 PROCESSING（可观测）
    broker_connection_retry_on_startup=True,  # 启动时 Redis 未就绪自动重连（docker compose 场景）
    timezone="Asia/Shanghai",
    enable_utc=True,
    # 周期任务：每 60s 扫一次"卡死超时仍 processing"的提交 → 复位重投（worker 崩溃运行期自愈）
    beat_schedule={
        "reclaim-stuck-every-60s": {
            "task": "flowinvoice.reclaim_stuck",
            "schedule": schedule(run_every=60.0),
        },
        # 周期任务（#62 审批 SLA）：每 60s 扫一次挂起单据 → 超时限催办/升级（时限在 policy yaml，
        #   阈值小时级，60s 扫描粒度足够且与 reclaim 同节奏；开销=每轮列 in_review 单据数，量级很小）
        "sla-sweep-every-60s": {
            "task": "flowinvoice.sla_sweep",
            "schedule": schedule(run_every=60.0),
        },
    },
)
