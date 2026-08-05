from __future__ import annotations

import hashlib
import json
import re
from typing import Any
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from contentai.agent.external_content import (
    looks_like_instruction_injection,
    sanitize_external_text,
)
from contentai.agent.workflows.deep_research import ContentEvidenceInvalidError


class ResearchFinalSelection(BaseModel):
    """The only model-authored input accepted for a research final response."""

    model_config = ConfigDict(extra="forbid")

    claim_ids: list[str] = Field(min_length=1, max_length=16)


def parse_research_identity(
    package_id: Any,
    topic_hash: Any,
) -> tuple[str, str] | None:
    """Return a complete research identity or reject an ambiguous partial identity."""
    normalized_package_id = str(package_id or "").strip()
    normalized_topic_hash = str(topic_hash or "").strip()
    if bool(normalized_package_id) != bool(normalized_topic_hash):
        raise ContentEvidenceInvalidError
    if not normalized_package_id:
        return None
    return normalized_package_id, normalized_topic_hash


def build_supported_research_evidence(research_package: Any) -> dict[str, Any]:
    """Build the canonical, durable evidence that a research final may select from."""
    sources: list[dict[str, str]] = []
    source_by_id: dict[str, dict[str, str]] = {}
    for raw_source in getattr(research_package, "sources", []):
        if not isinstance(raw_source, dict) or bool(raw_source.get("isolated")):
            continue
        source_id = sanitize_external_text(raw_source.get("source_id"), max_chars=80)
        url = sanitize_external_text(raw_source.get("url"), max_chars=2000)
        parsed_url = urlparse(url)
        if (
            not source_id
            or source_id in source_by_id
            or re.fullmatch(r"[A-Za-z0-9_-]+", source_id) is None
            or parsed_url.scheme not in {"http", "https"}
            or not parsed_url.hostname
        ):
            continue
        title = sanitize_external_text(raw_source.get("title"), max_chars=500)
        summary = sanitize_external_text(
            raw_source.get("summary") or raw_source.get("snippet"), max_chars=2000
        )
        publisher = sanitize_external_text(raw_source.get("source"), max_chars=200)
        if looks_like_instruction_injection("\n".join((title, summary, publisher))):
            continue
        source = {"source_id": source_id, "url": url}
        if title:
            source["title"] = title
        if summary:
            source["summary"] = summary
        if publisher:
            source["publisher"] = publisher
        source_by_id[source_id] = source
        sources.append(source)

    claims: list[dict[str, Any]] = []
    seen_claim_ids: set[str] = set()

    def append_claim(kind: str, value: Any) -> None:
        if not isinstance(value, dict):
            return
        source_ids = sorted(
            {
                str(source_id).strip()
                for source_id in value.get("source_ids", [])
                if str(source_id).strip()
            }
        )
        if not source_ids or any(source_id not in source_by_id for source_id in source_ids):
            return
        claim = sanitize_external_text(
            value.get("text") if kind == "conclusion" else value.get("claim"),
            max_chars=1200 if kind == "conclusion" else 500,
        )
        evidence = sanitize_external_text(value.get("evidence"), max_chars=900)
        if not claim or looks_like_instruction_injection("\n".join((claim, evidence))):
            return
        claim_id = _stable_claim_id(claim, source_ids)
        if claim_id in seen_claim_ids:
            return
        seen_claim_ids.add(claim_id)
        claims.append(
            {
                "claim_id": claim_id,
                "kind": kind,
                "claim": claim,
                "evidence": evidence,
                "source_ids": source_ids,
            }
        )

    package_data = getattr(research_package, "package_data", None)
    if isinstance(package_data, dict):
        append_claim("conclusion", package_data.get("core_conclusion"))
        findings = package_data.get("findings")
        if isinstance(findings, list):
            for finding in findings:
                append_claim("finding", finding)
    package_id = str(getattr(research_package, "id", "") or "").strip()
    topic_hash = str(getattr(research_package, "topic_hash", "") or "").strip()
    if not package_id or not topic_hash or not claims:
        raise ContentEvidenceInvalidError
    return {
        "research_pack_id": package_id,
        "topic_hash": topic_hash,
        "topic": sanitize_external_text(getattr(research_package, "topic", ""), max_chars=500),
        "claims": claims,
        "sources": sources,
    }


def coerce_research_final_selection(value: Any) -> ResearchFinalSelection:
    if isinstance(value, ResearchFinalSelection):
        return value
    try:
        if isinstance(value, dict):
            return ResearchFinalSelection.model_validate(value)
        model_dump = getattr(value, "model_dump", None)
        if callable(model_dump):
            return ResearchFinalSelection.model_validate(model_dump())
    except ValidationError as exc:
        raise ContentEvidenceInvalidError from exc
    raise ContentEvidenceInvalidError


def validate_research_final_selection(
    value: Any,
    *,
    evidence: dict[str, Any],
) -> ResearchFinalSelection:
    selection = coerce_research_final_selection(value)
    claims = evidence.get("claims") if isinstance(evidence, dict) else None
    if not isinstance(claims, list):
        raise ContentEvidenceInvalidError
    available_ids = {
        str(claim.get("claim_id") or "").strip()
        for claim in claims
        if isinstance(claim, dict) and str(claim.get("claim_id") or "").strip()
    }
    claim_ids = [str(claim_id).strip() for claim_id in selection.claim_ids]
    if (
        not claim_ids
        or len(claim_ids) != len(set(claim_ids))
        or any(claim_id not in available_ids for claim_id in claim_ids)
    ):
        raise ContentEvidenceInvalidError
    selection.claim_ids = claim_ids
    return selection


def render_deterministic_research_answer(
    evidence: dict[str, Any],
    claim_ids: list[str],
) -> str:
    selection = validate_research_final_selection({"claim_ids": claim_ids}, evidence=evidence)
    claims_by_id = {
        str(claim.get("claim_id")): claim
        for claim in evidence["claims"]
        if isinstance(claim, dict)
    }
    source_by_id = {
        str(source.get("source_id")): source
        for source in evidence.get("sources", [])
        if isinstance(source, dict)
    }
    selected_claims: list[dict[str, Any]] = []
    for claim_id in selection.claim_ids:
        claim = claims_by_id.get(claim_id)
        if not isinstance(claim, dict):
            raise ContentEvidenceInvalidError
        selected_claims.append(claim)

    conclusion = next(
        (claim for claim in selected_claims if claim.get("kind") == "conclusion"),
        None,
    )
    findings = [claim for claim in selected_claims if claim.get("kind") != "conclusion"]
    lines = ["## 选题研究摘要"]
    if conclusion is not None:
        lines.extend(
            [
                "",
                "### 核心结论",
                _research_claim_text(conclusion, source_by_id),
            ]
        )
    if findings:
        lines.extend(["", "### 关键事实"])
        for index, claim in enumerate(findings, start=1):
            lines.append(f"{index}. {_research_claim_text(claim, source_by_id)}")
    if conclusion is None and not findings:
        raise ContentEvidenceInvalidError
    return "\n".join(lines)


def _research_claim_text(
    claim: dict[str, Any],
    source_by_id: dict[str, dict[str, Any]],
) -> str:
    citations: list[str] = []
    for source_id in claim.get("source_ids", []):
        source = source_by_id.get(str(source_id))
        url = str(source.get("url") or "").strip() if isinstance(source, dict) else ""
        if not url:
            raise ContentEvidenceInvalidError
        citations.append(f"[{_source_link_label(source)}]({url})")
    text = str(claim.get("claim") or "").strip()
    if not text or not citations:
        raise ContentEvidenceInvalidError
    evidence = str(claim.get("evidence") or "").strip()
    details = f"\n   {evidence}" if evidence else ""
    return f"{text}{details}\n   - 参考资料：{'、'.join(citations)}"


def _source_link_label(source: dict[str, Any]) -> str:
    """Render trusted source metadata without exposing internal source identifiers."""
    title = sanitize_external_text(source.get("title"), max_chars=96)
    publisher = sanitize_external_text(source.get("publisher"), max_chars=80)
    if title:
        label = title
    elif publisher:
        label = publisher
    else:
        label = (urlparse(str(source.get("url") or "")).hostname or "查看来源").lower()
    return label.replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")


def build_research_final_proof(evidence: dict[str, Any], claim_ids: list[str]) -> dict[str, Any]:
    selection = validate_research_final_selection({"claim_ids": claim_ids}, evidence=evidence)
    package_id = str(evidence.get("research_pack_id") or "").strip()
    topic_hash = str(evidence.get("topic_hash") or "").strip()
    if not package_id or not topic_hash:
        raise ContentEvidenceInvalidError
    answer = render_deterministic_research_answer(evidence, selection.claim_ids)
    digest_payload = {
        "package_id": package_id,
        "topic_hash": topic_hash,
        "claim_ids": selection.claim_ids,
        "answer": answer,
    }
    digest = hashlib.sha256(
        json.dumps(
            digest_payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return {
        "package_id": package_id,
        "topic_hash": topic_hash,
        "claim_ids": selection.claim_ids,
        "digest": digest,
    }


def validate_research_final_proof(proof: Any, *, evidence: dict[str, Any]) -> str:
    """Validate proof fields against durable evidence and return its expected public answer."""
    if not isinstance(proof, dict) or set(proof) != {
        "package_id",
        "topic_hash",
        "claim_ids",
        "digest",
    }:
        raise ContentEvidenceInvalidError
    claim_ids = proof.get("claim_ids")
    if not isinstance(claim_ids, list) or not all(isinstance(item, str) for item in claim_ids):
        raise ContentEvidenceInvalidError
    expected = build_research_final_proof(evidence, claim_ids)
    if proof != expected:
        raise ContentEvidenceInvalidError
    return render_deterministic_research_answer(evidence, claim_ids)


def _stable_claim_id(claim: str, source_ids: list[str]) -> str:
    payload = {
        "claim": re.sub(r"\s+", " ", claim).strip().casefold(),
        "source_ids": sorted(source_ids),
    }
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return f"clm_{digest}"


__all__ = [
    "ResearchFinalSelection",
    "build_research_final_proof",
    "build_supported_research_evidence",
    "coerce_research_final_selection",
    "parse_research_identity",
    "render_deterministic_research_answer",
    "validate_research_final_proof",
    "validate_research_final_selection",
]
