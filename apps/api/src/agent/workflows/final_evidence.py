from __future__ import annotations

from typing import Any

from agent.workflows.deep_research import ContentEvidenceInvalidError
from pydantic import BaseModel, Field, ValidationError


class ResearchBackedClaim(BaseModel):
    """One factual claim the final research response exposes to the user."""

    text: str = Field(min_length=1, max_length=1200)
    source_ids: list[str] = Field(min_length=1, max_length=8)


class ResearchBackedFinalResponse(BaseModel):
    """Internal-only final-response envelope for an execution-local research package."""

    answer: str = Field(min_length=1, max_length=12000)
    claims: list[ResearchBackedClaim] = Field(min_length=1, max_length=16)


def coerce_research_backed_final_response(value: Any) -> ResearchBackedFinalResponse:
    if isinstance(value, ResearchBackedFinalResponse):
        return value
    try:
        if isinstance(value, dict):
            return ResearchBackedFinalResponse.model_validate(value)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return ResearchBackedFinalResponse.model_validate(model_dump())
    except ValidationError as exc:
        raise ContentEvidenceInvalidError from exc
    raise ContentEvidenceInvalidError


def validate_research_backed_final_response(
    value: Any,
    *,
    allowed_source_ids: set[str],
) -> ResearchBackedFinalResponse:
    response = coerce_research_backed_final_response(value)
    if not allowed_source_ids:
        raise ContentEvidenceInvalidError
    for claim in response.claims:
        source_ids = [str(source_id).strip() for source_id in claim.source_ids]
        if not source_ids or any(source_id not in allowed_source_ids for source_id in source_ids):
            raise ContentEvidenceInvalidError
        claim.source_ids = list(dict.fromkeys(source_ids))
    return response


__all__ = [
    "ResearchBackedClaim",
    "ResearchBackedFinalResponse",
    "coerce_research_backed_final_response",
    "validate_research_backed_final_response",
]
