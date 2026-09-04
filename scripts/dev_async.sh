#!/usr/bin/env bash
# dev_async.sh —— 一键起 Celery worker + beat（异步模式，docs/06 §2 / README「运行异步模式」）
# 前置：1) .env 设 FLOWINVOICE_ASYNC=1；2) docker compose 起 redis（broker）。
# 作用：worker 消费 submissions 队列执行发票处理；-B 内嵌 beat 驱动周期回收（reclaim_stuck 每 60s 扫卡死任务）。
# 分开起亦可：celery -A app.celery_app worker -P solo --loglevel=INFO 与 celery -A app.celery_app beat。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CELERY_BIN="$ROOT/.venv/bin/celery"
if [ ! -x "$CELERY_BIN" ]; then
  echo "未找到 $CELERY_BIN —— 请先创建 .venv 并 pip install -r requirements.txt" >&2
  exit 1
fi

echo "== FlowInvoice async worker + beat（Ctrl+C 退出）=="
exec "$CELERY_BIN" -A app.celery_app worker -P solo -B --loglevel=INFO
