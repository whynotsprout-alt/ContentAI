from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator
from typing import Any

from api.dependencies import AuthContextDep, ConversationServiceDep, SessionDep
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from models.schemas import (
    AgentMessageRequest,
    ChatRequest,
    StreamEventV2,
    UserReplyRequest,
)
from services.errors import (
    AccountNotFoundError,
    ActiveExecutionExistsError,
    ChatSessionNotFoundError,
    ExecutionNotResumableError,
    ExecutionResumeValueRequiredError,
)

router = APIRouter()


@router.post("/chat/sessions/{session_id}/messages/stream")
def stream_session_message(
    session_id: str,
    payload: ChatRequest,
    session: SessionDep,
    auth: AuthContextDep,
    service: ConversationServiceDep,
) -> StreamingResponse:
    try:
        events = service.create_and_run_stream(
            session,
            AgentMessageRequest(session_id=session_id, message=payload.message),
            auth,
        )
        return _event_stream_response(events)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=400, detail="Unknown account") from exc
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc
    except ActiveExecutionExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/chat/sessions/{session_id}/resume/stream")
def stream_resume_session(
    session_id: str,
    payload: UserReplyRequest,
    session: SessionDep,
    auth: AuthContextDep,
    service: ConversationServiceDep,
) -> StreamingResponse:
    try:
        events = service.resume_and_run_stream(session, session_id, payload, auth)
        return _event_stream_response(events)
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc
    except ActiveExecutionExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExecutionNotResumableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExecutionResumeValueRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _event_stream_response(events: Iterator[tuple[str, dict[str, Any]]]) -> StreamingResponse:
    async def stream():
        while True:
            item = await asyncio.to_thread(_next_event, events)
            if item is None:
                break
            event_name, payload = item
            try:
                stream_event = _to_stream_event_v2(
                    event_name=event_name,
                    payload=payload if isinstance(payload, dict) else {},
                )
            except ValueError:
                continue
            event_payload = stream_event.model_dump(mode="json")
            event_type = stream_event.type
            yield _encode_sse_event(event_type, event_payload)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _next_event(events: Iterator[tuple[str, dict[str, Any]]]) -> tuple[str, dict[str, Any]] | None:
    try:
        return next(events)
    except StopIteration:
        return None


def _to_stream_event_v2(event_name: str, payload: dict[str, Any]) -> StreamEventV2:
    normalized_event = (event_name or "").strip()
    if normalized_event not in {"token", "tool"}:
        raise ValueError(f"Unsupported stream event type: {event_name}")

    normalized_payload = payload if isinstance(payload, dict) else {}
    content = normalized_payload.get("content")
    if content is None:
        content = ""
    elif not isinstance(content, str):
        content = json.dumps(content, ensure_ascii=False)
    name = normalized_payload.get("name")
    normalized_name = str(name).strip() if isinstance(name, str) and name.strip() else None

    return StreamEventV2(
        type=normalized_event,
        content=str(content),
        name=normalized_name,
    )


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
