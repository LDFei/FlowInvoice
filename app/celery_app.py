# app/celery_app.py —— Celery 实例（broker=Redis；任务注册在 app/tasks/）
# 业务：#52 异步执行层：提交→处理→挂起 进 worker，API 提交即返回 pending（docs/06 §2）
# 启动：celery -A app.celery_app worker -P solo（Windows 无 prefork；Linux 可省 -P solo）
from celery import Celery

from app.core.config import REDIS_DSN

celery_app = Celery(
    "flowinvoice",
    broker=REDIS_DSN,
    include=["app.tasks.submit_task"],  # 启动时自动导入注册任务（避免 send_task 按名字串）
)

celery_app.conf.update(
    task_track_started=True,              # worker 开始执行即标记 PROCESSING（可观测）
    broker_connection_retry_on_startup=True,  # 启动时 Redis 未就绪自动重连（docker compose 场景）
    timezone="Asia/Shanghai",
    enable_utc=True,
)
