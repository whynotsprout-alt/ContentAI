from __future__ import annotations

import asyncio
import json
from typing import Any

from api.dependencies import SessionDep
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from models.schemas import (
    ChatSessionDetail,
    ChatSessionSummary,
    ChatUserMessageCreate,
    ChatUserMessageResponse,
    CreateSessionResponse,
    RunCreateRequest,
    RunResponse,
)
from services.chat_service import (
    ActiveRunExistsError,
    ChatSessionNotFoundError,
    RunNotFoundError,
    chat_service,
)
from services.errors import AccountNotFoundError
from sqlmodel import Session

router = APIRouter()

SSE_POLL_INTERVAL_SECONDS = 0.5
SSE_HEARTBEAT_INTERVAL_SECONDS = 15.0


@router.post("/chat/sessions", response_model=CreateSessionResponse)
def create_session(session: SessionDep) -> CreateSessionResponse:
    return chat_service.create_session(session)


@router.get("/chat/sessions", response_model=list[ChatSessionSummary])
def list_sessions(session: SessionDep) -> list[ChatSessionSummary]:
    return chat_service.list_sessions(session)


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionDetail)
def get_session(session_id: str, session: SessionDep) -> ChatSessionDetail:
    try:
        return chat_service.get_session(session, session_id)
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc


@router.post("/chat/runs", response_model=ChatUserMessageResponse)
def create_run(payload: RunCreateRequest, session: SessionDep) -> ChatUserMessageResponse:
    try:
        return chat_service.create_run(session, payload)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=400, detail="Unknown account") from exc
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc
    except ActiveRunExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post(
    "/chat/sessions/{session_id}/messages",
    response_model=ChatUserMessageResponse,
)
def create_session_message(
    session_id: str,
    payload: ChatUserMessageCreate,
    session: SessionDep,
) -> ChatUserMessageResponse:
    return create_run(
        RunCreateRequest(
            session_id=session_id,
            account_id=payload.account_id,
            message=payload.message,
        ),
        session=session,
    )


@router.get("/chat/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, session: SessionDep) -> RunResponse:
    try:
        return chat_service.get_run(session, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc


@router.get("/chat/runs/{run_id}/events")
async def run_events(
    run_id: str,
    request: Request,
    session: SessionDep,
    after_event_id: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    try:
        chat_service.run_status(session, run_id)
    except RunNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Run not found") from exc

    bind = session.get_bind()

    async def stream():
        last_event_id = _initial_event_id(after_event_id, request)
        last_heartbeat_at = 0.0
        while True:
            with Session(bind) as fresh:
                rows = chat_service.event_rows_for_run(
                    fresh,
                    run_id=run_id,
                    after_event_id=last_event_id,
                )
                encoded_rows, last_event_id = _encode_event_rows(rows, last_event_id)
                for encoded in encoded_rows:
                    yield encoded

                if chat_service.is_terminal_run(fresh, run_id):
                    yield _encode_sse_event(
                        "close",
                        {"run_id": run_id, "status": chat_service.run_status(fresh, run_id)},
                    )
                    break

            now = asyncio.get_running_loop().time()
            if now - last_heartbeat_at >= SSE_HEARTBEAT_INTERVAL_SECONDS:
                last_heartbeat_at = now
                yield _encode_sse_event("heartbeat", {"run_id": run_id})
            await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/chat/sessions/{session_id}/events")
async def session_events(
    session_id: str,
    request: Request,
    session: SessionDep,
    account_id: str = Query(min_length=1),
    after_event_id: int | None = Query(default=None, ge=0),
) -> StreamingResponse:
    try:
        chat_service.get_session(session, session_id)
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc

    bind = session.get_bind()

    async def stream():
        last_event_id = _initial_event_id(after_event_id, request)
        last_heartbeat_at = 0.0
        while True:
            with Session(bind) as fresh:
                rows = chat_service.event_rows_for_session(
                    fresh,
                    session_id=session_id,
                    account_id=account_id,
                    after_event_id=last_event_id,
                )
                encoded_rows, last_event_id = _encode_event_rows(rows, last_event_id)
                for encoded in encoded_rows:
                    yield encoded

            now = asyncio.get_running_loop().time()
            if now - last_heartbeat_at >= SSE_HEARTBEAT_INTERVAL_SECONDS:
                last_heartbeat_at = now
                yield _encode_sse_event(
                    "heartbeat",
                    {"session_id": session_id, "after_event_id": last_event_id},
                )
            await asyncio.sleep(SSE_POLL_INTERVAL_SECONDS)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _initial_event_id(after_event_id: int | None, request: Request) -> int:
    if after_event_id is not None:
        return after_event_id
    header = request.headers.get("last-event-id")
    if header is None:
        return 0
    try:
        return max(int(header), 0)
    except ValueError:
        return 0


def _encode_event_rows(rows: list[Any], last_event_id: int) -> tuple[list[str], int]:
    encoded_rows: list[str] = []
    for row in rows:
        payload = chat_service.event_payload(row)
        if payload is None:
            continue
        event_id = int(payload["id"])
        last_event_id = event_id
        encoded_rows.append(
            _encode_sse_event(
                str(payload["event"]),
                payload["data"],  # type: ignore[arg-type]
                event_id=event_id,
            )
        )
    return encoded_rows, last_event_id


def _encode_sse_event(
    event: str,
    data: dict[str, Any],
    *,
    event_id: str | int | None = None,
) -> str:
    if not event or "\n" in event or "\r" in event:
        raise ValueError("SSE event name must be a single non-empty line")
    lines: list[str] = []
    if event_id is not None:
        event_id_text = str(event_id)
        if "\n" in event_id_text or "\r" in event_id_text:
            raise ValueError("SSE event id must be a single line")
        lines.append(f"id: {event_id_text}")
    lines.append(f"event: {event}")
    encoded = json.dumps(data, ensure_ascii=False)
    for data_line in encoded.splitlines() or ["{}"]:
        lines.append(f"data: {data_line}")
    lines.append("")
    return "\n".join(lines) + "\n"
