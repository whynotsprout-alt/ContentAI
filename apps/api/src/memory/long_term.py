from __future__ import annotations

import re
from datetime import datetime
from hashlib import sha1
from typing import Any

from memory.repository import MemoryRepository, normalize_memory_kind
from memory.retriever import extract_memory_candidates
from memory.types import MemoryEntry
from models.enums import MemorySourceType
from pydantic import BaseModel, Field

SENSITIVE_PATTERNS = (
    re.compile(
        r"\b(?:api[\s_-]*key|client[\s_-]*secret|access[\s_-]*token|"
        r"private[\s_-]*key|secret|password|passwd|token|bearer)\b",
        re.I,
    ),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]{12,}\b"),
)
MIN_MEMORY_CONFIDENCE = 0.75
TRANSIENT_TASK_PATTERNS = (
    re.compile(r"(?:接着|继续|延续|回到).{0,12}(?:上一条|上次|前文|这个任务|该任务)", re.I),
    re.compile(
        r"(?:正在|本次|这次|下一步|已完成|完成了).{0,24}(?:出稿|文案|口播稿|脚本|选题|任务|素材)",
        re.I,
    ),
    re.compile(r"(?:上一条|上次|前文).{0,24}(?:出稿|文案|口播稿|脚本|选题|任务|素材)", re.I),
    re.compile(r"(?:果切|热点).{0,24}(?:出稿|口播稿|继续|下一步|已完成)", re.I),
)


class MemoryCandidate(BaseModel):
    kind: str = Field(default="semantic", max_length=40)
    content: str = Field(default="", max_length=1000)
    confidence: float = Field(default=0, ge=0, le=1)
    importance_score: float = Field(default=0, ge=0, le=1)
    reason: str = Field(default="", max_length=500)


class MemoryExtractionResult(BaseModel):
    memories: list[MemoryCandidate] = Field(default_factory=list, max_length=8)


class LongTermMemory:
    def __init__(self, repository: MemoryRepository) -> None:
        self.repository = repository

    def remember(
        self,
        agent_id: str,
        content: str,
        *,
        user_id: str,
        session_id: str | None = None,
        kind: str = "semantic",
        payload: dict[str, Any] | None = None,
        key: str | None = None,
        confidence: float = 1.0,
        importance_score: float = 0.0,
        source_type: str | MemorySourceType | None = None,
        source_message_id: str | None = None,
        source_execution_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryEntry:
        normalized = content.strip()
        if not normalized:
            raise ValueError("memory content cannot be empty")
        if is_sensitive_memory(normalized):
            raise ValueError("memory content appears to contain sensitive credentials")
        if is_transient_task_memory(normalized):
            raise ValueError("memory content is transient task progress")
        memory_kind = normalize_memory_kind(kind)
        memory_key = key or self._stable_key(memory_kind, normalized)
        normalized_payload = payload or {}
        entry = self.repository.upsert(
            memory_key,
            content=normalized[:1000],
            kind=memory_kind,
            payload=normalized_payload,
            user_id=user_id,
            agent_id=agent_id,
            session_id=None,
            confidence=confidence,
            importance_score=importance_score,
            source_type=source_type or normalized_payload.get("source"),
            source_session_id=session_id,
            source_message_id=source_message_id,
            source_execution_id=source_execution_id,
            expires_at=expires_at,
        )
        return entry

    def remember_from_user_message(
        self,
        agent_id: str,
        message: str,
        *,
        user_id: str,
        session_id: str | None = None,
        source_message_id: str | None = None,
        source_execution_id: str | None = None,
    ) -> list[MemoryEntry]:
        entries: list[MemoryEntry] = []
        for kind, content in extract_memory_candidates(message):
            entries.append(
                self.remember(
                    agent_id,
                    content,
                    user_id=user_id,
                    session_id=session_id,
                    kind=kind,
                    payload={"source": "user_message"},
                    source_type=MemorySourceType.user_message,
                    source_message_id=source_message_id,
                    source_execution_id=source_execution_id,
                )
            )
        return entries

    def remember_after_turn(
        self,
        *,
        agent_id: str,
        account_name: str,
        account_positioning: str,
        user_message: str,
        assistant_response: str,
        tool_results: list[str],
        model_gateway: Any,
        user_id: str,
        session_id: str | None = None,
        source_message_id: str | None = None,
        source_execution_id: str | None = None,
        callbacks: list[Any] | None = None,
    ) -> list[MemoryEntry]:
        existing = self.list_all(agent_id, user_id=user_id, limit=20)
        prompt = _memory_extraction_prompt(
            account_name=account_name,
            account_positioning=account_positioning,
            existing_memories=existing,
            user_message=user_message,
            assistant_response=assistant_response,
            tool_results=tool_results,
        )
        model = model_gateway.build_structured_output_model(MemoryExtractionResult)
        try:
            result = model.invoke(prompt, config={"callbacks": callbacks or []})
        except TypeError as exc:
            if "config" not in str(exc):
                raise
            result = model.invoke(prompt)
        candidates = _coerce_extraction_result(result)
        entries: list[MemoryEntry] = []
        for candidate in candidates:
            content = candidate.content.strip()
            kind = candidate.kind.strip() or "semantic"
            if candidate.confidence < MIN_MEMORY_CONFIDENCE:
                continue
            if not content or is_sensitive_memory(content) or is_transient_task_memory(content):
                continue
            entries.append(
                self.remember(
                    agent_id,
                    content,
                    user_id=user_id,
                    session_id=session_id,
                    kind=kind[:40],
                    payload={
                        "source": "turn_summary",
                        "scope": {
                            "user_id": user_id,
                            "agent_id": agent_id,
                            "session_id": session_id,
                            "execution_id": source_execution_id,
                        },
                        "confidence": candidate.confidence,
                        "importance_score": candidate.importance_score,
                        "reason": candidate.reason,
                    },
                    confidence=candidate.confidence,
                    importance_score=candidate.importance_score,
                    source_type=MemorySourceType.turn_summary,
                    source_message_id=source_message_id,
                    source_execution_id=source_execution_id,
                )
            )
        return entries

    def recall(
        self,
        agent_id: str,
        query: str,
        *,
        user_id: str,
        limit: int = 8,
    ) -> list[MemoryEntry]:
        return self.repository.search(
            query,
            limit=limit,
            user_id=user_id,
            agent_id=agent_id,
        )

    def list_all(
        self,
        agent_id: str,
        *,
        user_id: str,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        return self.repository.list_scope(
            limit=limit,
            user_id=user_id,
            agent_id=agent_id,
        )

    @staticmethod
    def _stable_key(kind: str, content: str) -> str:
        digest = sha1(f"{kind}:{content}".encode()).hexdigest()[:16]
        return f"{kind}_{digest}"


def is_sensitive_memory(content: str) -> bool:
    return any(pattern.search(content) for pattern in SENSITIVE_PATTERNS)


def is_transient_task_memory(content: str) -> bool:
    """Reject per-turn work state; only durable user/account facts belong here."""
    normalized = re.sub(r"\s+", " ", content or "").strip()
    return bool(
        normalized and any(pattern.search(normalized) for pattern in TRANSIENT_TASK_PATTERNS)
    )


def _coerce_extraction_result(result: Any) -> list[MemoryCandidate]:
    if isinstance(result, MemoryExtractionResult):
        return result.memories
    if isinstance(result, dict):
        raw_memories = result.get("memories", [])
    else:
        raw_memories = getattr(result, "memories", [])
    if not isinstance(raw_memories, list):
        return []
    candidates: list[MemoryCandidate] = []
    for item in raw_memories:
        try:
            candidate = (
                item if isinstance(item, MemoryCandidate) else MemoryCandidate.model_validate(item)
            )
            candidates.append(candidate)
        except Exception:
            continue
    return candidates


def _memory_extraction_prompt(
    *,
    account_name: str,
    account_positioning: str,
    existing_memories: list[MemoryEntry],
    user_message: str,
    assistant_response: str,
    tool_results: list[str],
) -> str:
    existing_text = (
        "\n".join(f"- [{item.kind}] {item.content}" for item in existing_memories) or "None"
    )
    tool_text = "\n\n".join(tool_results)[:4000] or "None"
    return (
        "Decide whether this completed conversation turn contains durable user/account "
        "preferences, goals, profile facts, or stable account operating constraints worth "
        "remembering. "
        "Never save temporary task state, prior-turn progress, draft status, a completed draft, "
        "a next-step suggestion, or instructions such as 'continue the previous item'. "
        "Return only structured data matching the schema. Do not include secrets, passwords, "
        "API keys, bearer tokens, one-time codes, private credentials, or transient task details. "
        "Prefer concise Chinese memory statements when the source content is Chinese. "
        "Use confidence 0.0-1.0 and include only memories useful in future conversations.\n\n"
        f"Account name:\n{account_name}\n\n"
        f"Account positioning:\n{account_positioning[:1200]}\n\n"
        f"Existing memories:\n{existing_text}\n\n"
        f"User message:\n{user_message[:4000]}\n\n"
        "Assistant response (use only to identify durable preferences/constraints, "
        f"never task progress):\n{assistant_response[:6000]}\n\n"
        f"Tool results summary:\n{tool_text}"
    )
