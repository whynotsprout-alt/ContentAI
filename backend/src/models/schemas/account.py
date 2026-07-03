from core.hotspot_sources import DEFAULT_HOTSPOT_SOURCES, normalize_hotspot_sources
from models.schemas.base import InputSchemaBase, SchemaBase
from pydantic import Field, constr, field_validator

AccountId = constr(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$")
ShortText = constr(min_length=1, max_length=120, strip_whitespace=True)
PositioningText = constr(min_length=1, max_length=4000, strip_whitespace=True)
RequiredPromptText = constr(min_length=1, max_length=12000, strip_whitespace=True)


class AccountSummary(SchemaBase):
    id: AccountId
    name: ShortText
    positioning: PositioningText
    topic_scoring_prompt: RequiredPromptText
    content_creation_prompt: RequiredPromptText
    hotspot_sources: list[str] = Field(default_factory=lambda: list(DEFAULT_HOTSPOT_SOURCES))

    @field_validator("hotspot_sources")
    @classmethod
    def validate_hotspot_sources(cls, value: list[str]) -> list[str]:
        return normalize_hotspot_sources(value)


class AccountDetail(AccountSummary):
    pass


class AccountCreate(InputSchemaBase):
    name: ShortText
    positioning: PositioningText
    topic_scoring_prompt: RequiredPromptText
    content_creation_prompt: RequiredPromptText
    hotspot_sources: list[str] = Field(default_factory=lambda: list(DEFAULT_HOTSPOT_SOURCES))

    @field_validator("hotspot_sources")
    @classmethod
    def validate_hotspot_sources(cls, value: list[str]) -> list[str]:
        return normalize_hotspot_sources(value)


class AccountUpdate(InputSchemaBase):
    name: ShortText | None = None
    positioning: PositioningText | None = None
    topic_scoring_prompt: RequiredPromptText | None = None
    content_creation_prompt: RequiredPromptText | None = None
    hotspot_sources: list[str] | None = None

    @field_validator("hotspot_sources")
    @classmethod
    def validate_hotspot_sources(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        return normalize_hotspot_sources(value)
