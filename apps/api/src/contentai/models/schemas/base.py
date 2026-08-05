from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    field_serializer,
    field_validator,
    model_validator,
)


def require_aware_datetime(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("datetime must be timezone-aware")
    return value


AwareDatetime = Annotated[datetime, AfterValidator(require_aware_datetime)]


def _reject_naive_datetimes(value: Any) -> None:
    if isinstance(value, datetime):
        require_aware_datetime(value)
    elif isinstance(value, BaseModel):
        for field_name in type(value).model_fields:
            _reject_naive_datetimes(getattr(value, field_name))
    elif isinstance(value, dict):
        for item in value.values():
            _reject_naive_datetimes(item)
    elif isinstance(value, list | tuple | set):
        for item in value:
            _reject_naive_datetimes(item)


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

    @model_validator(mode="after")
    def reject_naive_datetimes_recursively(self) -> "InputSchemaBase":
        for field_name in type(self).model_fields:
            _reject_naive_datetimes(getattr(self, field_name))
        return self
