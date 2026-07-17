from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, field_serializer, field_validator


class SchemaBase(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    @field_validator("*", mode="before")
    @classmethod
    def strip_strings(cls, value: Any) -> Any:
        if isinstance(value, str):
            trimmed = value.strip()
            if value and not trimmed:
                raise ValueError("string cannot be blank or whitespace")
            return trimmed
        return value

    @field_serializer("*", when_used="json")
    def serialize_utc_datetimes(self, value: Any) -> Any:
        if not isinstance(value, datetime):
            return value
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("API datetimes must be timezone-aware")
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


class InputSchemaBase(SchemaBase):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
