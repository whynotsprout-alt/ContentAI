from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from functools import wraps
from inspect import Signature, signature
from typing import Any

from api.dependencies import ConversationServiceDep, CurrentUserDep, RequestContextDep, SessionDep
from core.rate_limit import RateLimitRule, RateLimitUnavailable
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from models.schemas import (
    AgentMessageRequest,
    ChatExecutionResponse,
    ChatRequest,
    ChatSessionDetail,
    ChatSessionListResponse,
    ChatUserMessageResponse,
    CreateSessionRequest,
    CreateSessionResponse,
    MessageListRequest,
    StreamEventV3,
    UserReplyRequest,
)
from pydantic import ValidationError
from services.errors import (
    ActiveExecutionExistsError,
    AgentNotFoundError,
    ChatSessionNotFoundError,
    ExecutionNotFoundError,
    IdempotencyKeyConflictError,
    IdempotencyPayloadMismatchError,
    InvalidCursorError,
    InvalidStreamCursorError,
    RunInterruptStaleError,
    SessionAgentMismatchError,
    StreamingDegradedError,
    StreamReplayExpiredError,
    StreamReplayGapError,
)
from services.execution_resume import public_interrupt, public_interrupt_from_projection

router = APIRouter(prefix="/chat")
SSE_HEARTBEAT_SECONDS = 15.0
SSE_QUEUE_SIZE = 256
SSE_BACKPRESSURE_TIMEOUT_SECONDS = 5.0
_SSE_DONE = object()
logger = logging.getLogger(__name__)

ServiceHttpError = (
    AgentNotFoundError
    | ActiveExecutionExistsError
    | ChatSessionNotFoundError
    | ExecutionNotFoundError
    | IdempotencyKeyConflictError
    | IdempotencyPayloadMismatchError
    | InvalidCursorError
    | InvalidStreamCursorError
    | SessionAgentMismatchError
    | RunInterruptStaleError
    | StreamReplayExpiredError
    | StreamReplayGapError
    | StreamingDegradedError
)
SERVICE_HTTP_ERRORS = (
    AgentNotFoundError,
    ActiveExecutionExistsError,
    ChatSessionNotFoundError,
    ExecutionNotFoundError,
    IdempotencyKeyConflictError,
    IdempotencyPayloadMismatchError,
    InvalidCursorError,
    InvalidStreamCursorError,
    SessionAgentMismatchError,
    RunInterruptStaleError,
    StreamReplayExpiredError,
    StreamReplayGapError,
    StreamingDegradedError,
)


def translate_service_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    func_signature: Signature = signature(func)

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except SERVICE_HTTP_ERRORS as exc:
            context = _extract_service_error_context(func_signature, args, kwargs)
            raise _http_exception_for_service_error(exc, **context) from exc

    return wrapper


@router.post("/sessions", response_model=CreateSessionResponse)
@translate_service_errors
def create_session(
    payload: CreateSessionRequest,
    session: SessionDep,
    auth: CurrentUserDep,
    request_context: RequestContextDep,
    service: ConversationServiceDep,
) -> CreateSessionResponse:
    return service.create_session(session, payload, auth)


@router.get("/sessions", response_model=ChatSessionListResponse)
@translate_service_errors
def list_sessions(
    session: SessionDep,
    auth: CurrentUserDep,
    request_context: RequestContextDep,
    service: ConversationServiceDep,
    agent_id: str | None = Query(default=None),
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> ChatSessionListResponse:
    return service.list_sessions(session, auth, agent_id=agent_id, cursor=cursor, limit=limit)


@router.get("/sessions/{session_id}", response_model=ChatSessionDetail)
@translate_service_errors
def get_session(
    session_id: str,
    session: SessionDep,
    auth: CurrentUserDep,
    request_context: RequestContextDep,
    service: ConversationServiceDep,
    request: Request,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
) -> ChatSessionDetail:
    if "before" in request.query_params:
        raise HTTPException(status_code=422, detail="before is no longer supported")
    try:
        history = MessageListRequest(limit=limit, cursor=cursor)
    except ValidationError as exc:
        raise InvalidCursorError("Invalid cursor") from exc
    return service.get_session(
        session,
        session_id,
        auth,
        history,
    )


@router.delete("/sessions/{session_id}", status_code=204)
@translate_service_errors
def delete_session(
    session_id: str,
    session: SessionDep,
    auth: CurrentUserDep,
    request_context: RequestContextDep,
    service: ConversationServiceDep,
) -> None:
    try:
        service.delete_session(
            session,
            session_id,
            auth,
            request_id=request_context.request_id or "",
        )
    except ChatSessionNotFoundError as exc:
        raise HTTPException(
            status_code=404,
            detail=_build_service_error_detail(
                code="CHAT_SESSION_NOT_FOUND",
                message="Chat session not found",
                request_id=request_context.request_id,
                session_id=session_id,
            ),
        ) from exc
    except ActiveExecutionExistsError as exc:
        raise HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="SESSION_HAS_ACTIVE_EXECUTION",
                message=str(exc),
                request_id=request_context.request_id,
                session_id=session_id,
            ),
        ) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail=_build_service_error_detail(
                code="SESSION_PERSISTENCE_CLEANUP_FAILED",
                message="Chat session persistence cleanup failed",
                request_id=request_context.request_id,
                session_id=session_id,
            ),
        ) from exc


@router.post(
    "/sessions/{session_id}/messages",
    response_model=ChatUserMessageResponse,
    status_code=202,
)
@translate_service_errors
def create_session_message(
    session_id: str,
    payload: ChatRequest,
    request: Request,
    session: SessionDep,
    auth: CurrentUserDep,
    request_context: RequestContextDep,
    service: ConversationServiceDep,
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
) -> ChatUserMessageResponse:
    _enforce_llm_rate_limit(request, auth)
    if (
        idempotency_key is not None
        and payload.idempotency_key is not None
        and idempotency_key.strip() != payload.idempotency_key
    ):
        raise IdempotencyKeyConflictError(
            "Idempotency-Key header and body values must match."
        )
    request_idempotency_key = idempotency_key or payload.idempotency_key
    return service.submit_user_message_background(
        session,
        AgentMessageRequest(
            session_id=session_id,
            message=payload.message,
            message_id=payload.message_id,
        ),
        auth,
        idempotency_key=request_idempotency_key,
        request_id=request_context.request_id,
    )


@router.get("/runs/{execution_id}/events")
@translate_service_errors
def replay_run_events(
    execution_id: str,
    session: SessionDep,
    auth: CurrentUserDep,
    request_context: RequestContextDep,
    service: ConversationServiceDep,
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
    after_sequence: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    execution = service.get_execution_status(session, execution_id, auth)
    cursor = (
        after_sequence
        if after_sequence is not None
        else _parse_event_cursor(last_event_id, execution_id)
    )
    events = service.replay_execution_events(
        session.get_bind(),
        execution_id,
        auth,
        after_sequence=cursor,
    )
    return _event_stream_response(
        events,
        session_id=execution.session_id,
        execution_id=execution_id,
        request_id=request_context.request_id,
    )


@router.get("/runs/{execution_id}/status", response_model=ChatExecutionResponse)
@translate_service_errors
def get_run_status(
    execution_id: str,
    session: SessionDep,
    auth: CurrentUserDep,
    request_context: RequestContextDep,
    service: ConversationServiceDep,
) -> ChatExecutionResponse:
    return service.get_execution_status(session, execution_id, auth)


@router.post("/runs/{execution_id}/cancel", response_model=ChatExecutionResponse)
@translate_service_errors
def cancel_run(
    execution_id: str,
    session: SessionDep,
    auth: CurrentUserDep,
    request_context: RequestContextDep,
    service: ConversationServiceDep,
) -> ChatExecutionResponse:
    return service.cancel_execution(session, execution_id, auth)


@router.post("/runs/{execution_id}/resume", response_model=ChatExecutionResponse)
@translate_service_errors
def resume_run(
    execution_id: str,
    payload: UserReplyRequest,
    request: Request,
    session: SessionDep,
    auth: CurrentUserDep,
    request_context: RequestContextDep,
    service: ConversationServiceDep,
) -> ChatExecutionResponse:
    _enforce_llm_rate_limit(request, auth)
    return service.resume_execution(
        session,
        execution_id,
        payload.interrupt_id,
        payload.decision,
        auth,
        request_id=request_context.request_id,
    )


def _enforce_llm_rate_limit(request: Request, auth: Any) -> None:
    limiter = request.app.state.rate_limiter
    try:
        limiter.check("llm_run:minute", auth.user_id, RateLimitRule(30, 60))
        limiter.check(
            "llm_run:day",
            auth.user_id,
            RateLimitRule(
                int(request.app.state.settings.agent.user_daily_run_limit),
                24 * 60 * 60,
            ),
        )
    except PermissionError as exc:
        raise HTTPException(status_code=429, detail=str(exc)) from exc
    except RateLimitUnavailable as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc


def _event_stream_response(
    events: Iterator[tuple[str, dict[str, Any]]],
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    execution_id: str | None = None,
) -> StreamingResponse:
    async def stream():
        event_queue: asyncio.Queue[tuple[str, dict[str, Any]] | Exception | object] = asyncio.Queue(
            maxsize=SSE_QUEUE_SIZE
        )
        loop = asyncio.get_running_loop()
        stop_requested = threading.Event()
        backpressure_disconnected = threading.Event()
        started_at = time.monotonic()
        consumer = threading.Thread(
            target=_consume_sync_events,
            args=(events, loop, event_queue, stop_requested, backpressure_disconnected),
            name="sse-event-consumer",
            daemon=True,
        )
        consumer.start()

        try:
            while True:
                if backpressure_disconnected.is_set():
                    logger.info(
                        "sse_backpressure_disconnect_total=1 execution_id=%s "
                        "queue_high_water=%s connection_duration_seconds=%.3f",
                        execution_id,
                        SSE_QUEUE_SIZE,
                        time.monotonic() - started_at,
                    )
                    return
                try:
                    item = await asyncio.wait_for(
                        event_queue.get(),
                        timeout=SSE_HEARTBEAT_SECONDS,
                    )
                except TimeoutError:
                    yield _encode_sse_event("heartbeat", {"schema_version": 3})
                    continue

                if item is _SSE_DONE:
                    break
                if isinstance(item, Exception):
                    error_payload = _stream_exception_payload(item)
                    yield _encode_sse_event(
                        "errors",
                        _stream_exception_event(execution_id, error_payload),
                    )
                    break

                event_name, payload = item
                try:
                    stream_event = _to_stream_event_v3(
                        event_name=event_name,
                        payload=payload if isinstance(payload, dict) else {},
                        request_id=request_id,
                        session_id=session_id,
                        thread_id=thread_id,
                        execution_id=execution_id,
                    )
                except ValueError:
                    continue
                event_payload = stream_event.model_dump(mode="json")
                yield _encode_sse_event(
                    stream_event.channel,
                    event_payload,
                    event_id=stream_event.event_id,
                )
        finally:
            stop_requested.set()

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _consume_sync_events(
    events: Iterator[tuple[str, dict[str, Any]]],
    loop: asyncio.AbstractEventLoop,
    event_queue: asyncio.Queue[tuple[str, dict[str, Any]] | Exception | object],
    stop_requested: threading.Event,
    backpressure_disconnected: threading.Event,
) -> None:
    try:
        while True:
            if stop_requested.is_set():
                break
            try:
                event = next(events)
            except StopIteration:
                break
            if not _submit_sse_item(loop, event_queue, event, stop_requested):
                backpressure_disconnected.set()
                return
    except Exception as exc:  # noqa: BLE001
        if not _submit_sse_item(loop, event_queue, exc, stop_requested):
            backpressure_disconnected.set()
    finally:
        if not backpressure_disconnected.is_set():
            _submit_sse_item(loop, event_queue, _SSE_DONE, stop_requested)


def _submit_sse_item(
    loop: asyncio.AbstractEventLoop,
    event_queue: asyncio.Queue[tuple[str, dict[str, Any]] | Exception | object],
    item: tuple[str, dict[str, Any]] | Exception | object,
    stop_requested: threading.Event,
) -> bool:
    if stop_requested.is_set():
        return False
    future = asyncio.run_coroutine_threadsafe(event_queue.put(item), loop)
    try:
        future.result(timeout=SSE_BACKPRESSURE_TIMEOUT_SECONDS)
        return True
    except TimeoutError:
        future.cancel()
        return False
    except Exception:
        return False


def _extract_service_error_context(
    func_signature: Signature,
    args: tuple[Any, ...],
    kwargs: dict[str, Any],
) -> dict[str, Any]:
    context: dict[str, Any] = {
        "request_id": None,
        "session_id": None,
        "thread_id": None,
        "execution_id": None,
    }

    bound = None
    try:
        bound = func_signature.bind_partial(*args, **kwargs)
    except Exception:
        bound = None

    request_id = None
    session_id = None
    thread_id = None
    execution_id = None

    values: list[Any] = []
    if bound is not None:
        values.extend(bound.arguments.values())
        request_context = bound.arguments.get("request_context")
        if request_context is not None:
            request_id = getattr(request_context, "request_id", None)
            session_id = getattr(request_context, "conversation_id", None)
        if execution_id is None:
            candidate_execution_id = bound.arguments.get("execution_id")
            if candidate_execution_id is not None and candidate_execution_id != "":
                execution_id = candidate_execution_id
        if session_id is None:
            session_id = bound.arguments.get("session_id")
            if session_id is None:
                session_id = bound.arguments.get("conversation_id")
        if thread_id is None:
            thread_id = bound.arguments.get("thread_id")
    values.extend(kwargs.values())

    if request_id is None:
        request_id = _extract_context_value(values, "request_id")
    if session_id is None:
        session_id = _extract_context_value(values, "session_id")
    if thread_id is None:
        thread_id = _extract_context_value(values, "thread_id")
    if execution_id is None:
        execution_id = _extract_context_value(values, "execution_id")

    context["request_id"] = _optional_string(request_id)
    context["session_id"] = _optional_string(session_id)
    context["thread_id"] = _optional_string(thread_id)
    context["execution_id"] = _optional_string(execution_id)
    return context


def _extract_context_value(values: list[Any], key: str) -> str | None:
    for value in values:
        if isinstance(value, str) and key in {"session_id", "execution_id"}:
            maybe_candidate = value if value else None
            if maybe_candidate and isinstance(maybe_candidate, str):
                return maybe_candidate
        if key == "thread_id":
            langgraph_thread_id = getattr(value, "langgraph_thread_id", None)
            if langgraph_thread_id:
                return str(langgraph_thread_id)
        context_value = getattr(value, key, None)
        if context_value:
            return str(context_value)
    return None


def _http_exception_for_service_error(
    exc: ServiceHttpError,
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    execution_id: str | None = None,
) -> HTTPException:
    if isinstance(exc, AgentNotFoundError):
        return HTTPException(
            status_code=400,
            detail=_build_service_error_detail(
                code="AGENT_NOT_FOUND",
                message="Unknown agent",
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, ChatSessionNotFoundError):
        return HTTPException(
            status_code=404,
            detail=_build_service_error_detail(
                code="CHAT_SESSION_NOT_FOUND",
                message="Chat session not found",
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, ExecutionNotFoundError):
        return HTTPException(
            status_code=404,
            detail=_build_service_error_detail(
                code="EXECUTION_NOT_FOUND",
                message="Execution not found",
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, InvalidCursorError):
        return HTTPException(
            status_code=422,
            detail=_build_service_error_detail(
                code="INVALID_CURSOR",
                message="Invalid cursor",
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, InvalidStreamCursorError):
        return HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="INVALID_STREAM_CURSOR",
                message=str(exc),
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, StreamReplayGapError):
        return HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="STREAM_REPLAY_GAP",
                message=str(exc),
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, StreamReplayExpiredError):
        return HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="STREAM_REPLAY_EXPIRED",
                message=str(exc),
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, StreamingDegradedError):
        return HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="STREAMING_DEGRADED",
                message=str(exc),
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, IdempotencyPayloadMismatchError):
        return HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="IDEMPOTENCY_PAYLOAD_MISMATCH",
                message=str(exc),
                request_id=request_id,
                session_id=session_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, IdempotencyKeyConflictError):
        return HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="IDEMPOTENCY_KEY_CONFLICT",
                message=str(exc),
                request_id=request_id,
                session_id=session_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, RunInterruptStaleError):
        return HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="RUN_INTERRUPT_STALE",
                message=str(exc),
                request_id=request_id,
                session_id=session_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, SessionAgentMismatchError):
        return HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="SESSION_AGENT_MISMATCH",
                message=str(exc),
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    if isinstance(exc, ActiveExecutionExistsError):
        return HTTPException(
            status_code=409,
            detail=_build_service_error_detail(
                code="SESSION_HAS_ACTIVE_EXECUTION",
                message=str(exc),
                request_id=request_id,
                session_id=session_id,
                thread_id=thread_id,
                execution_id=execution_id,
            ),
        )
    return HTTPException(
        status_code=500,
        detail=_build_service_error_detail(
            code="UNEXPECTED_SERVICE_ERROR",
            message="Unexpected service error",
            request_id=request_id,
            session_id=session_id,
            thread_id=thread_id,
            execution_id=execution_id,
        ),
    )


def _build_service_error_detail(
    *,
    code: str,
    message: str,
    request_id: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    execution_id: str | None = None,
) -> dict[str, str]:
    raw: dict[str, str | None] = {
        "code": code,
        "message": message,
        "request_id": request_id,
        "session_id": session_id,
        "thread_id": thread_id,
        "execution_id": execution_id,
    }
    return {k: v for k, v in raw.items() if v is not None}


def _to_stream_event_v3(
    event_name: str,
    payload: dict[str, Any],
    *,
    request_id: str | None = None,
    session_id: str | None = None,
    thread_id: str | None = None,
    execution_id: str | None = None,
) -> StreamEventV3:
    normalized_event = (event_name or "").strip()
    normalized_payload = payload if isinstance(payload, dict) else {}
    execution_value = _optional_string(normalized_payload.get("execution_id")) or _optional_string(
        execution_id
    )
    if not execution_value:
        raise ValueError("execution_id is required")
    sequence = normalized_payload.get("sequence")
    try:
        sequence_value = int(sequence)
    except (ValueError, TypeError) as exc:
        raise ValueError("Stream event has no sequence") from exc
    if sequence_value < 1:
        raise ValueError("Stream event sequence must be positive")
    semantic_name = normalized_payload.get("name")
    name = str(semantic_name).strip() if isinstance(semantic_name, str) else ""
    channel = _stream_channel(normalized_event, name)
    has_interrupt = "interrupt" in normalized_payload
    data = _public_stream_data(normalized_payload, channel=channel)
    if channel == "errors":
        data = _normalize_stream_error_payload(
            normalized_event=normalized_event, semantic_name=name, payload=data
        )
    timestamp = normalized_payload.get("timestamp")
    try:
        timestamp_value = (
            datetime.fromisoformat(str(timestamp)) if timestamp else datetime.now().astimezone()
        )
    except ValueError:
        timestamp_value = datetime.now().astimezone()
    return StreamEventV3(
        execution_id=execution_value,
        sequence=sequence_value,
        event_id=f"{execution_value}:{sequence_value}",
        channel=channel,
        namespace=(
            ()
            if has_interrupt
            else tuple(
                str(item)
                for item in normalized_payload.get("namespace", [])
                if str(item)
            )
            if isinstance(normalized_payload.get("namespace"), list | tuple)
            else ()
        ),
        attempt_id=_optional_string(normalized_payload.get("attempt_id")),
        message_id=_optional_string(normalized_payload.get("message_id")),
        tool_call_id=(
            None
            if has_interrupt
            else _optional_string(normalized_payload.get("tool_call_id"))
        ),
        timestamp=timestamp_value,
        data=data,
    )


def _stream_channel(event_name: str, semantic_name: str) -> str:
    if event_name in {"tool_start", "tool_progress", "tool_end"}:
        return "tools"
    if event_name == "error" or semantic_name in {"run_error", "execution_failed"}:
        return "errors"
    if semantic_name in {"run_interrupt", "execution_waiting_input"}:
        return "interrupts"
    if event_name == "state" or semantic_name.startswith(
        ("run_", "attempt_", "execution_", "agent_")
    ):
        return "lifecycle"
    if event_name == "token":
        return "messages"
    return "values"


def _is_stream_error_type(*, normalized_event: str, semantic_name: str) -> bool:
    normalized_name = (normalized_event or "").strip().lower()
    normalized_semantic = (semantic_name or "").strip().lower()
    return normalized_name == "error" or (
        normalized_name == "token" and normalized_semantic == "execution_failed"
    )


def _stream_exception_payload(exc: Exception) -> dict[str, str]:
    message = str(exc).strip()
    if isinstance(exc, StreamReplayGapError):
        code = "STREAM_REPLAY_GAP"
    elif isinstance(exc, StreamReplayExpiredError):
        code = "STREAM_REPLAY_EXPIRED"
    elif isinstance(exc, StreamingDegradedError):
        code = "STREAMING_DEGRADED"
    else:
        code = _coerce_error_code(exc.__class__.__name__) or "STREAM_EXCEPTION_ERROR"
    return {
        "name": "stream_exception",
        "code": code,
        "message": message or "Stream processing failed.",
    }


def _stream_exception_event(execution_id: str | None, payload: dict[str, str]) -> dict[str, Any]:
    # Errors emitted by the transport have no Redis sequence; they are not
    # acknowledged and deliberately carry no Last-Event-ID advancement.
    return {
        "schema_version": 3,
        "execution_id": execution_id or "",
        "channel": "errors",
        "data": payload,
    }


def _public_stream_data(payload: dict[str, Any], *, channel: str) -> dict[str, Any]:
    """Project only public fields; never relay graph state or credentials."""
    if "interrupt" in payload:
        projected: dict[str, Any] = {
            "interrupt": _sanitize_public_interrupt(payload["interrupt"]),
        }
        if payload.get("name") in {"run_interrupt", "execution_waiting_input"}:
            projected = {"name": payload["name"], **projected}
        return _bounded_public_stream_data(projected)

    blocked = {
        "configurable",
        "runtime",
        "context",
        "state",
        "messages",
        "memory",
        "api_key",
        "authorization",
        "token",
        "prompt",
        "system_prompt",
        "trace_id",
    }
    allowed = {
        "name",
        "content",
        "chunk",
        "done",
        "status",
        "error",
        "error_code",
        "code",
        "message",
        "message_type",
        "retryable",
        "tool_name",
        "tool_call_id",
        "interrupt",
        "marker",
        "progress",
        "state_truncated",
        "history_cursor",
    }
    projected = {
        key: value for key, value in payload.items() if key in allowed and key not in blocked
    }
    return _bounded_public_stream_data(projected)


def _bounded_public_stream_data(projected: dict[str, Any]) -> dict[str, Any]:
    safe = _json_safe_stream_data(projected)
    encoded = json.dumps(safe, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= 512 * 1024:
        return safe
    return {"state_truncated": True, "summary": "Public stream payload exceeded 512KiB"}


def _sanitize_public_interrupt(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    if "interrupts" in value:
        sanitized = public_interrupt(value)
    else:
        sanitized = public_interrupt_from_projection(value)
    return sanitized.model_dump(exclude_none=True) if sanitized is not None else None


def _normalize_stream_error_payload(
    *,
    normalized_event: str,
    semantic_name: str,
    payload: dict[str, Any],
) -> dict[str, Any]:
    error_code = _coerce_error_code(
        payload.get("error_code") or payload.get("code") or payload.get("errorCode")
    )
    normalized_event_name = (normalized_event or "").strip().lower()
    normalized_semantic_name = (semantic_name or "").strip().lower()
    if not error_code:
        if normalized_event_name == "error":
            error_code = "AGENT_ERROR"
        elif normalized_event_name == "token" and normalized_semantic_name == "execution_failed":
            error_code = "AGENT_EXECUTION_FAILED"
        else:
            error_code = "STREAM_EVENT_ERROR"

    payload["code"] = error_code

    raw_error_message = payload.get("message")
    if not raw_error_message:
        raw_error_message = payload.get("error")
    if not raw_error_message:
        raw_error_message = payload.get("content")
    if isinstance(raw_error_message, str) and raw_error_message.strip():
        payload["message"] = raw_error_message.strip()
    return payload


def _coerce_error_code(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    normalized = "".join(ch if ch.isalnum() else "_" for ch in value.strip())
    return normalized.strip("_")


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_event_cursor(value: str | None, execution_id: str) -> int:
    if value is None or not value.strip():
        return 0
    raw = value.strip()
    if ":" in raw:
        incoming_execution_id, raw_sequence = raw.rsplit(":", 1)
        if incoming_execution_id != execution_id:
            raise HTTPException(
                status_code=422, detail="Last-Event-ID execution does not match request"
            )
        raw = raw_sequence
    try:
        return max(0, int(raw))
    except ValueError as exc:
        raise HTTPException(
            status_code=422, detail="Last-Event-ID must be a non-negative integer"
        ) from exc


def _json_safe_stream_data(payload: dict[str, Any]) -> dict[str, Any]:
    return json.loads(json.dumps(payload, ensure_ascii=False, default=str))


def _encode_sse_event(
    event: str,
    data: dict[str, Any],
    *,
    event_id: str | int | None = None,
) -> str:
    lines: list[str] = []
    if event_id is not None:
        event_id_text = str(event_id)
        lines.append(f"id: {event_id_text}")
    lines.append(f"event: {event}")
    encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    for data_line in encoded.splitlines() or ["{}"]:
        lines.append(f"data: {data_line}")
    lines.append("")
    return "\n".join(lines) + "\n"
