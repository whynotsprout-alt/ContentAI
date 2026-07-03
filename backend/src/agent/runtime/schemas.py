from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field


class AssistantResponse(BaseModel):
    message_type: Literal["markdown", "text"] = "markdown"
    content: str = Field(min_length=1, max_length=240000)
    metadata: dict[str, str | int | float | bool | None] = Field(default_factory=dict)


def validate_assistant_response(
    *,
    content: str,
    message_type: Literal["markdown", "text"] = "markdown",
    metadata: dict[str, str | int | float | bool | None] | None = None,
) -> AssistantResponse:
    return AssistantResponse(
        message_type=message_type,
        content=content[:240000],
        metadata=metadata or {},
    )
