from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from fastapi.responses import FileResponse, Response, StreamingResponse
from sqlmodel import Session, select

from articleforgeai.models.db import (
    Artifact,
    ChatMessage,
    ChatSession,
    PipelineRun,
    RunStatus,
    RunStep,
)
from articleforgeai.models.schemas import (
    AccountCreate,
    AccountDetail,
    AccountUpdate,
    ArtifactResponse,
    CreateSessionResponse,
    RunCreateRequest,
    RunCreateResponse,
    RunResponse,
    SystemConfig,
    SystemConfigUpdate,
    TopicSelectionRequest,
)
from articleforgeai.pipeline import pipeline_runner
from articleforgeai.pipeline.runner import INTERNAL_STEP_NAMES, STEP_LABELS
from articleforgeai.services.artifacts import artifact_store
from articleforgeai.services.catalog import catalog_service
from articleforgeai.services.database import get_session
from articleforgeai.services.hotspot_sources import RSS_SOURCES, TIKHUB_PLATFORMS
from articleforgeai.services.run_control import run_control_service
from articleforgeai.services.system_config import system_config_service

router = APIRouter(prefix="/api")


SessionDep = Annotated[Session, Depends(get_session)]


@router.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@router.get("/accounts")
def list_accounts():
    return catalog_service.list_accounts()


@router.get("/accounts/{account_id}")
def get_account(account_id: str):
    try:
        return catalog_service.get_account(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc


@router.get("/hotspot-platforms")
def list_hotspot_platforms() -> list[dict[str, str]]:
    platforms = [
        {"id": platform_id, "label": spec["label"], "source": "rss"}
        for platform_id, spec in RSS_SOURCES.items()
    ]
    platforms.extend(
        {
            "id": platform_id,
            "label": spec["label"],
            "source": "tikhub",
        }
        for platform_id, spec in TIKHUB_PLATFORMS.items()
    )
    platforms.append({"id": "aihot", "label": "AI HOT", "source": "aihot"})
    return platforms


@router.post("/accounts", response_model=AccountDetail, status_code=201)
def create_account(payload: AccountCreate) -> AccountDetail:
    try:
        return catalog_service.create_account(payload)
    except FileExistsError as exc:
        raise HTTPException(status_code=409, detail="Account already exists") from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/accounts/{account_id}", response_model=AccountDetail)
def update_account(account_id: str, payload: AccountUpdate) -> AccountDetail:
    try:
        return catalog_service.update_account(account_id, payload)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc


@router.get("/system-config")
def get_system_config() -> SystemConfig:
    return SystemConfig(**system_config_service.get_config())


@router.put("/system-config")
def update_system_config(payload: SystemConfigUpdate) -> SystemConfig:
    return SystemConfig(**system_config_service.update_config(payload.model_dump()))


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: str) -> None:
    try:
        catalog_service.delete_account(account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc


@router.post("/chat/sessions", response_model=CreateSessionResponse)
def create_session(session: SessionDep) -> CreateSessionResponse:
    chat = ChatSession()
    session.add(chat)
    session.commit()
    session.refresh(chat)
    return CreateSessionResponse(session_id=chat.id, title=chat.title)


@router.post("/runs", response_model=RunCreateResponse)
def create_run(
    payload: RunCreateRequest, background: BackgroundTasks, session: SessionDep
) -> RunCreateResponse:
    try:
        catalog_service.get_account(payload.account_id)
    except FileNotFoundError as exc:
        raise HTTPException(status_code=400, detail="Unknown account") from exc

    chat = session.get(ChatSession, payload.session_id) if payload.session_id else None
    if chat is None:
        chat = ChatSession(title=payload.message[:32])
        session.add(chat)
        session.commit()
        session.refresh(chat)

    session.add(ChatMessage(session_id=chat.id, role="user", content=payload.message))
    run = PipelineRun(
        session_id=chat.id,
        account_id=payload.account_id,
        user_message=payload.message,
    )
    session.add(run)
    session.commit()
    session.refresh(run)
    background.add_task(pipeline_runner.run, run.id)
    return RunCreateResponse(run_id=run.id, session_id=chat.id, status=run.status)


@router.get("/runs/{run_id}", response_model=RunResponse)
def get_run(run_id: str, session: SessionDep) -> RunResponse:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    artifacts = session.exec(select(Artifact).where(Artifact.run_id == run_id)).all()
    steps = session.exec(select(RunStep).where(RunStep.run_id == run_id)).all()
    return RunResponse(
        id=run.id,
        session_id=run.session_id,
        account_id=run.account_id,
        user_message=run.user_message,
        status=run.status,
        next_stage=run.next_stage,
        pending_payload=run.pending_payload,
        selected_topic_title=run.selected_topic_title,
        error=run.error,
        artifacts=[
            ArtifactResponse(
                id=item.id,
                kind=item.kind,
                title=item.title,
                media_type=item.media_type,
                summary=item.summary,
                url=f"/api/artifacts/{item.id}",
            )
            for item in artifacts
        ],
        steps=[
            {
                "name": step.name,
                "label": STEP_LABELS.get(step.name, step.label),
                "status": step.status,
                "error": step.error,
            }
            for step in steps
            if step.name not in INTERNAL_STEP_NAMES
        ],
    )


@router.get("/runs/{run_id}/events")
async def run_events(run_id: str, session: SessionDep) -> StreamingResponse:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    run_dir = Path(run.run_dir) if run.run_dir else artifact_store.run_dir(run_id)
    event_path = run_dir / "run_events.jsonl"

    async def stream():
        offset = 0
        while True:
            if event_path.exists():
                with event_path.open("r", encoding="utf-8") as handle:
                    handle.seek(offset)
                    while True:
                        line_start = handle.tell()
                        line = handle.readline()
                        if not line:
                            break
                        offset = handle.tell()
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            payload = json.loads(line)
                        except json.JSONDecodeError:
                            continue
                        yield f"id: {line_start}\n"
                        yield f"event: {payload['event']}\n"
                        yield f"data: {json.dumps(payload['data'], ensure_ascii=False)}\n\n"
                yield "event: heartbeat\n\ndata: {}\n\n"
            with Session(session.bind) as fresh:
                fresh_run = fresh.get(PipelineRun, run_id)
                if fresh_run and fresh_run.status in {
                    RunStatus.completed,
                    RunStatus.failed,
                    RunStatus.waiting_for_topic_confirmation,
                    RunStatus.waiting_for_research_confirmation,
                }:
                    yield "event: close\n"
                    close_data = json.dumps(
                        {"status": fresh_run.status},
                        ensure_ascii=False,
                    )
                    yield f"data: {close_data}\n\n"
                    break
            await asyncio.sleep(0.5)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache"},
    )


@router.post("/runs/{run_id}/select-topic")
def select_topic(
    run_id: str,
    payload: TopicSelectionRequest,
    background: BackgroundTasks,
    session: SessionDep,
) -> RunResponse:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RunStatus.waiting_for_topic_confirmation:
        raise HTTPException(
            status_code=409,
            detail="Run is not waiting for topic confirmation",
        )

    try:
        payload_data = run_control_service.parse_pending_payload(run)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if run_control_service.is_hotspot_confirmation(payload_data):
        run_control_service.resume(session, run, "filter_hotspots")
        background.add_task(pipeline_runner.run, run.id)
        return get_run(run_id, session)

    topics = payload_data
    if not isinstance(topics, list):
        raise HTTPException(status_code=409, detail="Invalid pending topic payload")
    if payload.topic_index >= len(topics) or payload.topic_index < 0:
        raise HTTPException(status_code=400, detail="topic_index out of range")

    selected = topics[payload.topic_index]
    if not isinstance(selected, dict):
        raise HTTPException(status_code=400, detail="Invalid topic payload format")

    run.selected_topic_title = selected.get("title")
    run.selected_topic_data = json.dumps(selected, ensure_ascii=False)
    run_control_service.resume(session, run, "deep_search_topic")
    background.add_task(pipeline_runner.run, run.id)
    return get_run(run_id, session)


@router.post("/runs/{run_id}/confirm-hotspots")
def confirm_hotspots(run_id: str, background: BackgroundTasks, session: SessionDep) -> RunResponse:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RunStatus.waiting_for_topic_confirmation:
        raise HTTPException(
            status_code=409,
            detail="Run is not waiting for hotspot confirmation",
        )

    try:
        payload_data = run_control_service.parse_pending_payload(run)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if not run_control_service.is_hotspot_confirmation(payload_data):
        raise HTTPException(
            status_code=409,
            detail="Run is not waiting for hotspot confirmation",
        )

    run_control_service.resume(session, run, "filter_hotspots")
    background.add_task(pipeline_runner.run, run.id)
    return get_run(run_id, session)


@router.post("/runs/{run_id}/confirm-research")
def confirm_research(run_id: str, background: BackgroundTasks, session: SessionDep) -> RunResponse:
    run = session.get(PipelineRun, run_id)
    if run is None:
        raise HTTPException(status_code=404, detail="Run not found")
    if run.status != RunStatus.waiting_for_research_confirmation:
        raise HTTPException(
            status_code=409,
            detail="Run is not waiting for research confirmation",
        )

    run_control_service.resume(session, run, "generate_content_draft")
    background.add_task(pipeline_runner.run, run.id)
    return get_run(run_id, session)


@router.get("/artifacts/{artifact_id}")
def get_artifact(artifact_id: str, session: SessionDep) -> Response:
    artifact = session.get(Artifact, artifact_id)
    if artifact is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    if artifact.path.startswith("virtual://"):
        return Response(
            content=artifact.summary,
            media_type=artifact.media_type,
            headers={"Content-Disposition": "inline"},
        )
    path = artifact_store.resolve(artifact)
    if not path.exists():
        raise HTTPException(status_code=404, detail="Artifact file missing")
    return FileResponse(
        path,
        media_type=artifact.media_type,
        filename=path.name,
        headers={"Content-Disposition": "inline"},
    )
