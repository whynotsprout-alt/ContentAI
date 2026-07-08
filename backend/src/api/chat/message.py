from __future__ import annotations

from api.dependencies import AuthContextDep, ConversationServiceDep, SessionDep
from fastapi import APIRouter, HTTPException
from models.schemas import (
    AgentMessageRequest,
    ChatRequest,
    ChatUserMessageResponse,
    UserReplyRequest,
)
from models.schemas import ChatSessionDetail
from services.errors import (
    AccountNotFoundError,
    ActiveExecutionExistsError,
    ChatSessionNotFoundError,
    ExecutionNotResumableError,
    ExecutionResumeValueRequiredError,
)

router = APIRouter()


@router.post("/chat/sessions/{session_id}/messages", response_model=ChatUserMessageResponse)
def create_session_message(
    session_id: str,
    payload: ChatRequest,
    session: SessionDep,
    auth: AuthContextDep,
    service: ConversationServiceDep,
) -> ChatUserMessageResponse:
    try:
        return service.create_and_run_background(
            session,
            AgentMessageRequest(session_id=session_id, message=payload.message),
            auth,
        )
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=400, detail="Unknown account") from exc
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc
    except ActiveExecutionExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/chat/sessions/{session_id}/resume", response_model=ChatSessionDetail)
def resume_session(
    session_id: str,
    payload: UserReplyRequest,
    session: SessionDep,
    auth: AuthContextDep,
    service: ConversationServiceDep,
) -> ChatSessionDetail:
    try:
        return service.resume_session(session, session_id, payload, auth)
    except ChatSessionNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Chat session not found") from exc
    except ActiveExecutionExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExecutionNotResumableError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ExecutionResumeValueRequiredError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
