# app/core/logging.py —— 企业级技术日志框架
# 业务：运行诊断日志（区别于业务审计，审计走 DB 表见 docs/06）。
#       能力：JSON 结构化（ELK/Loki 可采集）+ 关联追踪（contextvars 注入 request_id/invoice_no）
#             + 文件轮转（防磁盘无限增长）+ 级别/目录 env 可配 + 敏感信息脱敏。
import json
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from app.core.config import (
    LOG_BACKUP_COUNT,
    LOG_DIR,
    LOG_LEVEL,
    LOG_MAX_BYTES,
    PROJECT_ROOT,
)

# ================= 关联追踪（contextvars） =================
# 作用：同一报销链路（API → 图 → 适配器）的日志串到同一 request_id；
#       contextvars 随线程/异步任务自动传播（FastAPI threadpool 与 Celery worker 均生效）
_request_id: ContextVar[str] = ContextVar("request_id", default="")
_invoice_no: ContextVar[str] = ContextVar("invoice_no", default="")

# ================= 敏感信息脱敏 =================
# 作用：日志可能带出 URL（如 DSN/回调地址）里的口令，统一掩掉
_SECRET_PATTERN = re.compile(r"(://)([^:@/\s]+):([^@/\s]+)@")


def _redact(text: str) -> str:
    return _SECRET_PATTERN.sub(r"\1\2:****@", text)


class JsonFormatter(logging.Formatter):
    """结构化 JSON 日志（机器可读；异常堆栈 / extra 字段一并入包）"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "time": datetime.fromtimestamp(record.created).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "event": _redact(record.getMessage()),
        }
        if _request_id.get():
            payload["request_id"] = _request_id.get()
        if _invoice_no.get():
            payload["invoice_no"] = _invoice_no.get()
        # 调用方 extra={"fields": {...}} 注入的附加字段（invoice_no/amount/business_type 等）
        if getattr(record, "fields", None):
            payload.update(record.fields)
        if record.exc_info:
            payload["exc_info"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False)


class ConsoleFormatter(logging.Formatter):
    """控制台人类可读格式（带关联上下文，便于人工排查）"""

    def format(self, record: logging.LogRecord) -> str:
        ctx = []
        if _request_id.get():
            ctx.append(f"req={_request_id.get()}")
        if _invoice_no.get():
            ctx.append(f"inv={_invoice_no.get()}")
        base = super().format(record)
        return f"[{record.levelname}] {base}" + (f" ({' '.join(ctx)})" if ctx else "")


def setup_logging() -> None:
    """应用启动时装配日志：控制台（人类可读）+ 轮转文件（JSON）。幂等可重复调用。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    root = logging.getLogger()
    root.setLevel(LOG_LEVEL)
    root.handlers.clear()  # 幂等：重复 setup 不叠 handler

    console = logging.StreamHandler()
    console.setFormatter(ConsoleFormatter("%(message)s"))
    root.addHandler(console)

    file_handler = RotatingFileHandler(
        LOG_DIR / "flowinvoice.log",
        maxBytes=LOG_MAX_BYTES,
        backupCount=LOG_BACKUP_COUNT,
        encoding="utf-8",
    )
    file_handler.setFormatter(JsonFormatter())
    root.addHandler(file_handler)


def get_logger(name: str) -> logging.Logger:
    """模块级 logger（与 setup_logging 装配的 handler 联动）"""
    return logging.getLogger(name)


def set_log_context(*, request_id: str = "", invoice_no: str = "") -> None:
    """设置关联上下文（业务层在拿到 request_id/invoice_no 后调用）"""
    if request_id:
        _request_id.set(request_id)
    if invoice_no:
        _invoice_no.set(invoice_no)


def clear_log_context() -> None:
    """清理关联上下文（请求/任务结束时调用，防跨请求串号）"""
    _request_id.set("")
    _invoice_no.set("")


@contextmanager
def log_context(**fields):
    """作用域式设置：进入设置、退出恢复旧值（worker 每任务/请求结束时自动还原）"""
    old_request_id, old_invoice_no = _request_id.get(), _invoice_no.get()
    if fields.get("request_id"):
        _request_id.set(fields["request_id"])
    if fields.get("invoice_no"):
        _invoice_no.set(fields["invoice_no"])
    try:
        yield
    finally:
        _request_id.set(old_request_id)
        _invoice_no.set(old_invoice_no)


def log_event(logger: logging.Logger, level: int, event: str, **fields) -> None:
    """结构化事件日志：logger.<level>(event, extra={"fields": fields}) 的封装"""
    logger.log(level, event, extra={"fields": fields})


def log_error(logger: logging.Logger, event: str, exc_info=True, **fields) -> None:
    """异常错误日志：自动带堆栈 + 附加字段"""
    logger.error(event, extra={"fields": fields}, exc_info=exc_info)


def log_info(logger: logging.Logger, event: str, **fields) -> None:
    log_event(logger, logging.INFO, event, **fields)


def log_warning(logger: logging.Logger, event: str, **fields) -> None:
    log_event(logger, logging.WARNING, event, **fields)


def log_debug(logger: logging.Logger, event: str, **fields) -> None:
    log_event(logger, logging.DEBUG, event, **fields)
