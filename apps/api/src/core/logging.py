from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from core.config import Settings, get_settings
from core.paths import PROJECT_ROOT

_HANDLER_MARKER = "_contentai_log_handler"
_LOG_FORMAT = "%(asctime)s %(levelname)s [%(process)d] %(name)s: %(message)s"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(service_name: str, settings: Settings | None = None) -> Path:
    """Send one service's console and application logs to ``logs/<service>.log``.

    The function is safe to invoke more than once in a process: it replaces only
    handlers previously installed by ContentAI and leaves test/framework handlers alone.
    """

    settings = settings or get_settings()
    normalized_service = _normalize_service_name(service_name)
    log_path = PROJECT_ROOT / "logs" / f"{normalized_service}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    level = _log_level(settings.logging.level)
    formatter = logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT)
    root = logging.getLogger()
    root.setLevel(level)
    _remove_contentai_handlers(root)

    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(level)
    console_handler.setFormatter(formatter)
    setattr(console_handler, _HANDLER_MARKER, True)

    file_handler = RotatingFileHandler(
        log_path,
        maxBytes=settings.logging.max_bytes,
        backupCount=settings.logging.backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(level)
    file_handler.setFormatter(formatter)
    setattr(file_handler, _HANDLER_MARKER, True)

    root.addHandler(console_handler)
    root.addHandler(file_handler)
    _route_framework_loggers_to_root(level)
    logging.getLogger(__name__).info(
        "Logging configured for service=%s file=%s", normalized_service, log_path
    )
    return log_path


def _route_framework_loggers_to_root(level: int) -> None:
    # Uvicorn and Celery install non-propagating handlers by default. Routing them
    # through root gives each service one consistent file and format.
    for logger_name in (
        "uvicorn",
        "uvicorn.access",
        "uvicorn.error",
        "celery",
        "celery.task",
        "celery.redirected",
        "kombu",
    ):
        framework_logger = logging.getLogger(logger_name)
        framework_logger.handlers.clear()
        framework_logger.setLevel(level)
        framework_logger.propagate = True


def _remove_contentai_handlers(logger: logging.Logger) -> None:
    for handler in list(logger.handlers):
        if getattr(handler, _HANDLER_MARKER, False):
            logger.removeHandler(handler)
            handler.close()


def _log_level(value: str) -> int:
    candidate = str(value or "INFO").upper()
    level = logging.getLevelName(candidate)
    if not isinstance(level, int):
        raise ValueError(f"Unsupported log level: {value}")
    return level


def _normalize_service_name(value: str) -> str:
    normalized = "".join(
        character if character.isalnum() or character in {"-", "_"} else "-"
        for character in str(value).strip().lower()
    ).strip("-_")
    if not normalized:
        raise ValueError("service_name must contain a letter or number")
    return normalized


__all__ = ["configure_logging"]
