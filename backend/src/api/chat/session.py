from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from api.dependencies import AuthContextDep, ConversationServiceDep, SessionDep
from models.schemas import (
    ChatSessionDetail,
    ChatSessionSummary,
    CreateSessionRequest,
    CreateSessionResponse,
    MessageListRequest,
)
from services.errors import (
    ActiveExecutionExistsError,
    ChatSessionNotFoundError,
    InvalidCursorError,
    AccountNotFoundError,
)

router = APIRouter()


@router.post("/chat/sessions", response_model=CreateSessionResponse)
def create_session(
    payload: CreateSessionRequest,
    session: SessionDep,
    auth: AuthContextDep,
    service: ConversationServiceDep,
) -> CreateSessionResponse:
    try:
        return service.create_session(session, payload, auth)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=400, detail="Unknown account") from exc


@router.get("/chat/sessions", response_model=list[ChatSessionSummary])
def list_sessions(
    session: SessionDep,
    auth: AuthContextDep,
    service: ConversationServiceDep,
) -> list[ChatSessionSummary]:
    return service.list_sessions(session, auth)


@router.get("/chat/sessions/{session_id}", response_model=ChatSessionDetail)
def get_session(
    session_id: str,
    session: SessionDep,
    auth: AuthContextDep,
    service: ConversationServiceDep,
    cursor: str | None = Query(default=None),
    limit: int = Query(default=50, ge=1, le=200),
    before: str | None = Query(default=None),
) -> ChatSessionDetail:
    try:
        return service.get_session(
            session,
            session_id,
            auth,
            MessageListRequest(limit=limit, cursor=cursor, before=before),
        )
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc
    except InvalidCursorError as exc:
        raise HTTPException(status_code=422, detail="Invalid cursor") from exc


@router.delete("/chat/sessions/{session_id}", status_code=204)
def delete_session(
    session_id: str,
    session: SessionDep,
    auth: AuthContextDep,
    service: ConversationServiceDep,
) -> None:
    try:
        service.delete_session(session, session_id, auth)
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc
    except ActiveExecutionExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(
            status_code=500,
            detail="Chat session persistence cleanup failed",
        ) from exc


@router.post("/chat/sessions/{session_id}/cancel", response_model=ChatSessionDetail)
def cancel_session(
    session_id: str,
    session: SessionDep,
    auth: AuthContextDep,
    service: ConversationServiceDep,
) -> ChatSessionDetail:
    try:
        return service.cancel_session(session, session_id, auth)
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc
