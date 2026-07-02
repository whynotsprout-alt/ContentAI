from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator


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


class InputSchemaBase(SchemaBase):
    model_config = ConfigDict(from_attributes=True, extra="forbid")
