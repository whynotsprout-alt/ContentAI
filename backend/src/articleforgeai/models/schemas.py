from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class AccountSummary(BaseModel):
    id: str
    name: str
    description: str
    style_prompt: str = ""


class AccountDetail(AccountSummary):
    audience: str = ""
    preferred_directions: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    viral_patterns: list[str] = Field(default_factory=list)


class AccountCreate(BaseModel):
    id: str = Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
    name: str = Field(min_length=1, max_length=120)
    description: str = ""
    audience: str = ""
    preferred_directions: list[str] = Field(default_factory=list)
    boundaries: list[str] = Field(default_factory=list)
    viral_patterns: list[str] = Field(default_factory=list)
    style_prompt: str = ""
    hotspot_platforms: list[str] | None = None
    topic_filter_prompt: str | None = None
    topic_scoring_prompt: str | None = None


class AccountUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = None
    audience: str | None = None
    preferred_directions: list[str] | None = None
    boundaries: list[str] | None = None
    viral_patterns: list[str] | None = None
    style_prompt: str | None = None
    hotspot_platforms: list[str] | None = None
    topic_filter_prompt: str | None = None
    topic_scoring_prompt: str | None = None


class SystemConfig(BaseModel):
    hotspot_platforms: list[str] = Field(default_factory=list)
    topic_filter_prompt: str = ""
    topic_scoring_prompt: str = ""


class SystemConfigUpdate(SystemConfig):
    pass


class CreateSessionResponse(BaseModel):
    session_id: str
    title: str


class RunCreateRequest(BaseModel):
    session_id: str | None = None
    account_id: str
    message: str = Field(min_length=1, max_length=2000)


class RunCreateResponse(BaseModel):
    run_id: str
    session_id: str
    status: str


class ArtifactResponse(BaseModel):
    id: str
    kind: str
    title: str
    media_type: str
    summary: str
    url: str


class RunResponse(BaseModel):
    id: str
    session_id: str
    account_id: str
    user_message: str
    status: str
    next_stage: str
    pending_payload: str = ""
    selected_topic_title: str | None
    error: str
    artifacts: list[ArtifactResponse]
    steps: list[dict[str, Any]]


class TopicSelectionRequest(BaseModel):
    topic_index: int = Field(ge=0)
    confirm: bool | None = True
