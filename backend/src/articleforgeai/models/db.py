from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from uuid import uuid4

from sqlmodel import Field, SQLModel


def utcnow() -> datetime:
    return datetime.now(UTC)


def new_id(prefix: str) -> str:
    return f"{prefix}_{uuid4().hex[:16]}"


class RunStatus(StrEnum):
    queued = "queued"
    running = "running"
    waiting_for_topic_confirmation = "waiting_for_topic_confirmation"
    waiting_for_research_confirmation = "waiting_for_research_confirmation"
    completed = "completed"
    failed = "failed"


class StepStatus(StrEnum):
    pending = "pending"
    running = "running"
    completed = "completed"
    failed = "failed"


class Account(SQLModel, table=True):
    id: str = Field(primary_key=True)
    name: str
    description: str = ""
    audience: str = ""
    preferred_directions: str = Field(default="[]")
    boundaries: str = Field(default="[]")
    viral_patterns: str = Field(default="[]")
    style_prompt: str = ""
    raw_profile: str = Field(default="{}")
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ChatSession(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("ses"), primary_key=True)
    title: str = "新对话"
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class ChatMessage(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("msg"), primary_key=True)
    session_id: str = Field(index=True)
    role: str
    content: str
    created_at: datetime = Field(default_factory=utcnow)


class PipelineRun(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("run"), primary_key=True)
    session_id: str = Field(index=True)
    account_id: str = Field(index=True)
    user_message: str
    status: RunStatus = Field(default=RunStatus.queued, index=True)
    selected_topic_title: str | None = None
    selected_topic_data: str = ""
    next_stage: str = Field(default="analyze_intent", index=True)
    pending_payload: str = ""
    run_dir: str = ""
    error: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)


class RunStep(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("step"), primary_key=True)
    run_id: str = Field(index=True)
    name: str
    label: str
    status: StepStatus = Field(default=StepStatus.pending)
    started_at: datetime | None = None
    completed_at: datetime | None = None
    error: str = ""


class Artifact(SQLModel, table=True):
    id: str = Field(default_factory=lambda: new_id("art"), primary_key=True)
    run_id: str = Field(index=True)
    kind: str
    title: str
    path: str
    media_type: str = "application/json"
    summary: str = ""
    sha256: str = ""
    created_at: datetime = Field(default_factory=utcnow)


class SystemConfig(SQLModel, table=True):
    id: int = Field(default=1, primary_key=True)
    hotspot_platforms: str = Field(default="[]")
    topic_filter_prompt: str = ""
    topic_scoring_prompt: str = ""
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
