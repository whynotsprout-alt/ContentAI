from __future__ import annotations

from pydantic import Field, constr, field_validator

from contentai.core.hotspot_sources import normalize_hotspot_sources
from contentai.models.schemas.base import InputSchemaBase, SchemaBase

AgentId = constr(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
ShortText = constr(min_length=1, max_length=120, strip_whitespace=True)
DescriptionText = constr(max_length=4000, strip_whitespace=True)
RequiredPromptText = constr(min_length=1, max_length=30000, strip_whitespace=True)
OptionalPromptText = constr(max_length=30000, strip_whitespace=True)


class AgentVersionSummary(SchemaBase):
    id: AgentId
    agent_id: AgentId
    version: int
    topic_scoring_prompt: OptionalPromptText = ""
    content_prompt: RequiredPromptText
    hotspot_sources: list[str] = Field(default_factory=list)


class AgentProfileSummary(SchemaBase):
    id: AgentId
    name: ShortText
    description: DescriptionText = ""
    current_version: AgentVersionSummary | None = None


class AgentProfileDetail(AgentProfileSummary):
    pass


class AgentProfileCreate(InputSchemaBase):
    name: ShortText
    description: DescriptionText = ""
    topic_scoring_prompt: OptionalPromptText = ""
    content_prompt: RequiredPromptText
    hotspot_sources: list[str] = Field(default_factory=list)

    @field_validator("hotspot_sources")
    @classmethod
    def validate_hotspot_sources(cls, value: list[str]) -> list[str]:
        return normalize_hotspot_sources(value)


class AgentProfileUpdate(InputSchemaBase):
    name: ShortText | None = None
    description: DescriptionText | None = None


class AgentVersionCreate(InputSchemaBase):
    topic_scoring_prompt: OptionalPromptText = ""
    content_prompt: RequiredPromptText
    hotspot_sources: list[str] = Field(default_factory=list)

    @field_validator("hotspot_sources")
    @classmethod
    def validate_hotspot_sources(cls, value: list[str]) -> list[str]:
        return normalize_hotspot_sources(value)
