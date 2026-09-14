"""JSON 结构化日志、运行上下文和文件轮转。"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import logging.handlers
import os
from pathlib import Path

from ops.redaction import redact


_CONTEXT = contextvars.ContextVar("ops_log_context", default={})
_CONFIGURED = False


class JsonFormatter(logging.Formatter):
    def format(self, record):
        payload = {
            "timestamp": dt.datetime.now(dt.timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "event": getattr(record, "event", ""),
            "context": {
                **_CONTEXT.get(),
                **dict(getattr(record, "context", {}) or {}),
            },
        }
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(
            redact(payload, account=True),
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )


class PlainFormatter(logging.Formatter):
    def format(self, record):
        context = {
            **_CONTEXT.get(),
            **dict(getattr(record, "context", {}) or {}),
        }
        event = getattr(record, "event", "")
        suffix = ""
        if event:
            suffix += f" event={event}"
        if context:
            suffix += " " + json.dumps(
                redact(context, account=True),
                ensure_ascii=False,
                sort_keys=True,
                default=str,
            )
        base = super().format(record)
        return base + suffix


def configure_logging(
        *,
        level=None,
        log_file=None,
        json_logs=None,
        max_bytes=None,
        backup_count=None,
        force=False,
):
    """配置根日志；环境变量可覆盖参数。"""
    global _CONFIGURED
    if _CONFIGURED and not force:
        return logging.getLogger()
    level_name = (
        level or os.getenv("OPS_LOG_LEVEL")
        or os.getenv("LOG_LEVEL", "INFO")
    )
    json_enabled = _env_bool(
        "OPS_LOG_JSON",
        bool(json_logs) if json_logs is not None else True,
    )
    log_path = Path(
        log_file
        or os.getenv("OPS_LOG_FILE")
        or "logs/runtime.jsonl"
    )
    log_path.parent.mkdir(parents=True, exist_ok=True)
    max_size = int(
        max_bytes
        if max_bytes is not None
        else os.getenv("OPS_LOG_MAX_BYTES", 10 * 1024 * 1024)
    )
    backups = int(
        backup_count
        if backup_count is not None
        else os.getenv("OPS_LOG_BACKUP_COUNT", 10)
    )
    file_formatter = JsonFormatter() if json_enabled else PlainFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    console_formatter = PlainFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%H:%M:%S",
    )
    root = logging.getLogger()
    root.handlers.clear()
    root.setLevel(getattr(logging, str(level_name).upper(), logging.INFO))
    file_handler = logging.handlers.RotatingFileHandler(
        log_path,
        maxBytes=max(1024, max_size),
        backupCount=max(0, backups),
        encoding="utf-8",
    )
    file_handler.setFormatter(file_formatter)
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(console_formatter)
    root.addHandler(file_handler)
    root.addHandler(console_handler)
    _CONFIGURED = True
    return root


def bind_log_context(**values):
    current = dict(_CONTEXT.get())
    current.update(values)
    return _CONTEXT.set(current)


def reset_log_context(token):
    _CONTEXT.reset(token)


def log_event(logger, event, message, *, level=logging.INFO, **context):
    logger.log(
        level,
        message,
        extra={"event": event, "context": context},
    )


def _env_bool(key, default):
    value = os.getenv(key)
    if value is None or not value.strip():
        return bool(default)
    return value.strip().lower() in {"1", "true", "yes", "on"}
