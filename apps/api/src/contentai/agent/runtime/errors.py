from __future__ import annotations

from dataclasses import dataclass

import httpx

MODEL_STREAM_INTERRUPTED_MESSAGE = "模型服务的流式连接意外中断，请稍后重试。"
MODEL_STREAM_INTERRUPTED_CODE = "MODEL_STREAM_INTERRUPTED"
PUBLIC_RUNTIME_ERROR_CODES = {"CONTENT_EVIDENCE_INVALID"}


@dataclass(frozen=True)
class RuntimeErrorDetail:
    message: str
    code: str
    retryable: bool = False


def _normalize_runtime_error(exc: Exception) -> str:
    declared_code = str(getattr(exc, "code", "") or "").strip()
    if declared_code == "CONTENT_EVIDENCE_INVALID":
        return "Research evidence could not be validated."
    return "模型服务请求失败，请检查当前模型配置后重试。"


def normalize_runtime_error(exc: Exception) -> str:
    return classify_runtime_error(exc).message


def classify_runtime_error(exc: Exception) -> RuntimeErrorDetail:
    if is_retryable_model_stream_error(exc):
        return RuntimeErrorDetail(
            message=MODEL_STREAM_INTERRUPTED_MESSAGE,
            code=MODEL_STREAM_INTERRUPTED_CODE,
            retryable=True,
        )
    declared_code = str(getattr(exc, "code", "") or "").strip()
    if declared_code in PUBLIC_RUNTIME_ERROR_CODES:
        return RuntimeErrorDetail(
            message=_normalize_runtime_error(exc),
            code=declared_code,
        )
    return RuntimeErrorDetail(
        message=_normalize_runtime_error(exc),
        code=_generic_error_code(exc),
    )


def is_retryable_model_stream_error(exc: BaseException) -> bool:
    """Detect transport failures that occurred before a model invocation returned."""
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        if isinstance(current, httpx.RemoteProtocolError | httpx.ReadTimeout | httpx.ConnectError):
            return True
        current = current.__cause__ or current.__context__
    return False


def _generic_error_code(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return "AGENT_VALUE_ERROR"
    if isinstance(exc, RuntimeError):
        return "AGENT_RUNTIME_ERROR"
    if isinstance(exc, PermissionError):
        return "AGENT_PERMISSION_ERROR"
    if isinstance(exc, LookupError):
        return "AGENT_LOOKUP_ERROR"
    return f"AGENT_{exc.__class__.__name__.upper()}"
