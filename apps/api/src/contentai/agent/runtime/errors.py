from __future__ import annotations

import math
import random
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

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
    """Detect transient model failures that are safe for an outer invocation retry."""
    for current in _exception_chain(exc):
        if isinstance(
            current,
            httpx.TimeoutException
            | httpx.NetworkError
            | httpx.RemoteProtocolError
            | httpx.ProxyError,
        ):
            return True
        status_code = _exception_status_code(current)
        if status_code in {408, 409, 429} or (
            status_code is not None and 500 <= status_code <= 599
        ):
            return True
    return False


def model_retry_delay_seconds(
    exc: BaseException,
    *,
    attempt: int,
    base_seconds: float,
    max_seconds: float,
    random_value: float | None = None,
    now: datetime | None = None,
) -> float:
    """Return a bounded retry delay, honoring provider retry headers when present."""
    maximum = _finite_nonnegative(max_seconds)
    if maximum <= 0:
        return 0.0

    retry_after = _retry_after_seconds(exc, now=now)
    if retry_after is None:
        base = _finite_nonnegative(base_seconds)
        try:
            exponent = min(max(int(attempt) - 1, 0), 30)
        except (TypeError, ValueError, OverflowError):
            exponent = 0
        delay = min(maximum, base * (2**exponent))
    else:
        delay = min(maximum, retry_after)

    sample = random.random() if random_value is None else random_value
    try:
        bounded_sample = min(1.0, max(0.0, float(sample)))
    except (TypeError, ValueError, OverflowError):
        bounded_sample = 0.0
    if not math.isfinite(bounded_sample):
        bounded_sample = 0.0

    jitter_window = min(delay * 0.25, 1.0, maximum - delay)
    return delay + jitter_window * bounded_sample


def _exception_chain(exc: BaseException) -> Iterator[BaseException]:
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        yield current
        current = current.__cause__ or current.__context__


def _exception_status_code(exc: BaseException) -> int | None:
    for value in (
        getattr(exc, "status_code", None),
        getattr(getattr(exc, "response", None), "status_code", None),
    ):
        if isinstance(value, bool):
            continue
        if isinstance(value, int):
            return value
        if isinstance(value, str):
            normalized = value.strip()
            if normalized.isdecimal():
                return int(normalized)
    return None


def _retry_after_seconds(exc: BaseException, *, now: datetime | None) -> float | None:
    headers = [
        response_headers
        for current in _exception_chain(exc)
        if (
            response_headers := getattr(
                getattr(current, "response", None),
                "headers",
                None,
            )
        )
        is not None
    ]
    for response_headers in headers:
        milliseconds = _parse_nonnegative_number(
            _header_value(response_headers, "retry-after-ms")
        )
        if milliseconds is not None:
            return milliseconds / 1_000.0

    for response_headers in headers:
        value = _header_value(response_headers, "retry-after")
        seconds = _parse_nonnegative_number(value)
        if seconds is not None:
            return seconds
        if value is None:
            continue
        try:
            retry_at = parsedate_to_datetime(value)
        except (TypeError, ValueError, OverflowError):
            continue
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        current_time = now or datetime.now(UTC)
        if current_time.tzinfo is None:
            current_time = current_time.replace(tzinfo=UTC)
        return max(0.0, (retry_at - current_time).total_seconds())
    return None


def _header_value(headers: object, name: str) -> str | None:
    getter = getattr(headers, "get", None)
    if callable(getter):
        try:
            value = getter(name)
        except (TypeError, ValueError):
            value = None
        if value is not None:
            normalized = str(value).strip()
            return normalized or None

    items = getattr(headers, "items", None)
    if not callable(items):
        return None
    try:
        pairs = items()
    except (TypeError, ValueError):
        return None
    for key, value in pairs:
        if str(key).casefold() == name.casefold():
            normalized = str(value).strip()
            return normalized or None
    return None


def _parse_nonnegative_number(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number


def _finite_nonnegative(value: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    if not math.isfinite(number) or number < 0:
        return 0.0
    return number


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
