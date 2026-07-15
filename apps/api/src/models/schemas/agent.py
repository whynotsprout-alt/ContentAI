from __future__ import annotations

from typing import Any

from core.hotspot_sources import normalize_hotspot_sources
from models.enums import AgentStatus, AgentType
from models.schemas.base import InputSchemaBase, SchemaBase
from pydantic import Field, constr, field_validator

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
    graph_name: ShortText
    tools_config: dict[str, Any] = Field(default_factory=dict)
    memory_config: dict[str, Any] = Field(default_factory=dict)


class AgentProfileSummary(SchemaBase):
    id: AgentId
    tenant_id: str
    owner_user_id: str | None = None
    name: ShortText
    description: DescriptionText = ""
    agent_type: AgentType = AgentType.custom
    status: AgentStatus = AgentStatus.draft
    current_version: AgentVersionSummary | None = None


class AgentProfileDetail(AgentProfileSummary):
    versions: list[AgentVersionSummary] = Field(default_factory=list)


class AgentProfileCreate(InputSchemaBase):
    name: ShortText
    description: DescriptionText = ""
    agent_type: AgentType = AgentType.custom
    status: AgentStatus = AgentStatus.draft
    topic_scoring_prompt: OptionalPromptText = ""
    content_prompt: RequiredPromptText
    graph_name: ShortText = "default"
    tools_config: dict[str, Any] = Field(default_factory=dict)
    memory_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tools_config")
    @classmethod
    def validate_tools_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_tools_config(value)

    @field_validator("memory_config")
    @classmethod
    def validate_memory_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_config_object(value)


class AgentProfileUpdate(InputSchemaBase):
    name: ShortText | None = None
    description: DescriptionText | None = None
    agent_type: AgentType | None = None
    status: AgentStatus | None = None


class AgentVersionCreate(InputSchemaBase):
    topic_scoring_prompt: OptionalPromptText = ""
    content_prompt: RequiredPromptText
    graph_name: ShortText = "default"
    tools_config: dict[str, Any] = Field(default_factory=dict)
    memory_config: dict[str, Any] = Field(default_factory=dict)

    @field_validator("tools_config")
    @classmethod
    def validate_tools_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_tools_config(value)

    @field_validator("memory_config")
    @classmethod
    def validate_memory_config(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _validate_config_object(value)


def _validate_config_object(value: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("config must be an object")
    return value


def _validate_tools_config(value: dict[str, Any]) -> dict[str, Any]:
    config = dict(_validate_config_object(value))
    if "hotspot_sources" not in config:
        return config
    raw_sources = config["hotspot_sources"]
    if not isinstance(raw_sources, list | tuple):
        raise ValueError("tools_config.hotspot_sources must be an array")
    config["hotspot_sources"] = normalize_hotspot_sources(raw_sources)
    return config
