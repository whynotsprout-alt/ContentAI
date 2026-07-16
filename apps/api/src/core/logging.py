from __future__ import annotations

import logging
import sys

from core.config import Settings, get_settings

_HANDLER_MARKER = "_contentai_log_handler"
_DATE_FORMAT = "%Y-%m-%dT%H:%M:%S%z"


def configure_logging(service_name: str, settings: Settings | None = None) -> None:
    """Configure one process for container-native stdout logging."""

    settings = settings or get_settings()
    service = _normalize_service_name(service_name)
    level = _log_level(settings.logging.level)
    formatter = logging.Formatter(
        f"%(asctime)s service={service} level=%(levelname)s logger=%(name)s "
        "process=%(process)d message=%(message)s",
        datefmt=_DATE_FORMAT,
    )
    root = logging.getLogger()
    root.setLevel(level)
    _remove_contentai_handlers(root)

    handler = logging.StreamHandler(sys.stdout)
    handler.setLevel(level)
    handler.setFormatter(formatter)
    setattr(handler, _HANDLER_MARKER, True)
    root.addHandler(handler)
    _route_framework_loggers_to_root(level)
    logging.getLogger(__name__).info("Logging configured for service=%s", service)


def _route_framework_loggers_to_root(level: int) -> None:
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
