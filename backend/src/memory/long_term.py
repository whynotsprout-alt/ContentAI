from __future__ import annotations

import re
from datetime import datetime
from hashlib import sha1
from typing import Any

from memory.repository import MemoryRepository, normalize_memory_kind
from memory.retriever import extract_memory_candidates
from memory.types import MemoryEntry
from models.enums import MemoryScope
from pydantic import BaseModel, Field

SENSITIVE_PATTERNS = (
    re.compile(r"\b(?:api[_-]?key|secret|password|passwd|token|bearer)\b", re.I),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"\b[A-Za-z0-9_\-]{24,}\.[A-Za-z0-9_\-]{12,}\.[A-Za-z0-9_\-]{12,}\b"),
)
MIN_MEMORY_CONFIDENCE = 0.75


class MemoryCandidate(BaseModel):
    kind: str = Field(default="semantic", max_length=40)
    content: str = Field(default="", max_length=1000)
    confidence: float = Field(default=0, ge=0, le=1)
    importance_score: float = Field(default=0, ge=0, le=1)
    reason: str = Field(default="", max_length=500)


class MemoryExtractionResult(BaseModel):
    memories: list[MemoryCandidate] = Field(default_factory=list, max_length=8)


def long_term_store_namespace(
    tenant_id: str,
    user_id: str = "local-user",
    account_id: str | None = None,
) -> tuple[str, ...]:
    if account_id is None:
        account_id = tenant_id
        tenant_id = "local"
    return ("tenants", tenant_id, "users", user_id, "accounts", account_id, "long_term")


class LongTermMemory:
    def __init__(self, repository: MemoryRepository, store: Any) -> None:
        self.repository = repository
        self.store = store

    def remember(
        self,
        account_id: str,
        content: str,
        *,
        tenant_id: str = "local",
        user_id: str = "local-user",
        session_id: str | None = None,
        kind: str = "semantic",
        payload: dict[str, Any] | None = None,
        key: str | None = None,
        confidence: float = 1.0,
        importance_score: float = 0.0,
        source_type: str = "",
        source_message_id: str | None = None,
        source_execution_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryEntry:
        normalized = content.strip()
        if not normalized:
            raise ValueError("memory content cannot be empty")
        if is_sensitive_memory(normalized):
            raise ValueError("memory content appears to contain sensitive credentials")
        memory_kind = normalize_memory_kind(kind)
        store_namespace = long_term_store_namespace(tenant_id, user_id, account_id)
        memory_key = key or self._stable_key(memory_kind, normalized)
        normalized_payload = payload or {}
        entry = self.repository.upsert(
            memory_key,
            content=normalized[:1000],
            kind=memory_kind,
            payload=normalized_payload,
            tenant_id=tenant_id,
            user_id=user_id,
            account_id=account_id,
            session_id=None,
            memory_scope=MemoryScope.long_term,
            confidence=confidence,
            importance_score=importance_score,
            source_type=source_type or str(normalized_payload.get("source") or ""),
            source_session_id=session_id,
            source_message_id=source_message_id,
            source_execution_id=source_execution_id,
            expires_at=expires_at,
        )
        self.store.put(store_namespace, memory_key, _store_value(entry))
        return entry

    def remember_from_user_message(
        self,
        account_id: str,
        message: str,
        *,
        tenant_id: str = "local",
        user_id: str = "local-user",
        session_id: str | None = None,
        source_message_id: str | None = None,
        source_execution_id: str | None = None,
    ) -> list[MemoryEntry]:
        entries: list[MemoryEntry] = []
        for kind, content in extract_memory_candidates(message):
            entries.append(
                self.remember(
                    account_id,
                    content,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    kind=kind,
                    payload={"source": "user_message"},
                    source_type="user_message",
                    source_message_id=source_message_id,
                    source_execution_id=source_execution_id,
                )
            )
        return entries

    def remember_after_turn(
        self,
        *,
        account_id: str,
        account_name: str,
        account_positioning: str,
        user_message: str,
        assistant_response: str,
        tool_results: list[str],
        model_gateway: Any,
        tenant_id: str = "local",
        user_id: str = "local-user",
        session_id: str | None = None,
        source_message_id: str | None = None,
        source_execution_id: str | None = None,
    ) -> list[MemoryEntry]:
        existing = self.list_all(account_id, tenant_id=tenant_id, user_id=user_id, limit=20)
        prompt = _memory_extraction_prompt(
            account_name=account_name,
            account_positioning=account_positioning,
            existing_memories=existing,
            user_message=user_message,
            assistant_response=assistant_response,
            tool_results=tool_results,
        )
        result = model_gateway.build_structured_output_model(MemoryExtractionResult).invoke(prompt)
        candidates = _coerce_extraction_result(result)
        entries: list[MemoryEntry] = []
        for candidate in candidates:
            content = candidate.content.strip()
            kind = candidate.kind.strip() or "semantic"
            if candidate.confidence < MIN_MEMORY_CONFIDENCE:
                continue
            if not content or is_sensitive_memory(content):
                continue
            entries.append(
                self.remember(
                    account_id,
                    content,
                    tenant_id=tenant_id,
                    user_id=user_id,
                    session_id=session_id,
                    kind=kind[:40],
                    payload={
                        "source": "turn_summary",
                        "confidence": candidate.confidence,
                        "importance_score": candidate.importance_score,
                        "reason": candidate.reason,
                    },
                    confidence=candidate.confidence,
                    importance_score=candidate.importance_score,
                    source_type="turn_summary",
                    source_message_id=source_message_id,
                    source_execution_id=source_execution_id,
                )
            )
        return entries

    def recall(
        self,
        account_id: str,
        query: str,
        *,
        tenant_id: str = "local",
        user_id: str = "local-user",
        limit: int = 8,
    ) -> list[MemoryEntry]:
        store_namespace = long_term_store_namespace(tenant_id, user_id, account_id)
        self._bootstrap_store(
            tenant_id=tenant_id,
            user_id=user_id,
            account_id=account_id,
        )
        db_entries = self.repository.search(
            query,
            limit=limit,
            tenant_id=tenant_id,
            user_id=user_id,
            account_id=account_id,
            memory_scope=MemoryScope.long_term,
        )
        seen = {entry.key for entry in db_entries}
        for item in self.store.search(store_namespace, query=query, limit=limit):
            if item.key in seen:
                continue
            entry = _entry_from_store_item(item, tenant_id=tenant_id, user_id=user_id)
            if entry is None:
                continue
            db_entries.append(entry)
            seen.add(item.key)
        return db_entries[:limit]

    def list_all(
        self,
        account_id: str,
        *,
        tenant_id: str = "local",
        user_id: str = "local-user",
        limit: int = 20,
    ) -> list[MemoryEntry]:
        self._bootstrap_store(
            tenant_id=tenant_id,
            user_id=user_id,
            account_id=account_id,
        )
        return self.repository.list_scope(
            limit=limit,
            tenant_id=tenant_id,
            user_id=user_id,
            account_id=account_id,
            memory_scope=MemoryScope.long_term,
        )

    def _bootstrap_store(
        self,
        *,
        tenant_id: str,
        user_id: str,
        account_id: str,
    ) -> None:
        store_namespace = long_term_store_namespace(tenant_id, user_id, account_id)
        for entry in self.repository.list_scope(
            limit=100,
            tenant_id=tenant_id,
            user_id=user_id,
            account_id=account_id,
            memory_scope=MemoryScope.long_term,
        ):
            if self.store.get(store_namespace, entry.key) is None:
                self.store.put(store_namespace, entry.key, _store_value(entry))

    @staticmethod
    def _stable_key(kind: str, content: str) -> str:
        digest = sha1(f"{kind}:{content}".encode()).hexdigest()[:16]
        return f"{kind}_{digest}"


def is_sensitive_memory(content: str) -> bool:
    return any(pattern.search(content) for pattern in SENSITIVE_PATTERNS)


def _store_value(entry: MemoryEntry) -> dict[str, Any]:
    return {
        "content": entry.content,
        "kind": entry.kind,
        "payload": entry.payload,
        "confidence": entry.confidence,
        "importance_score": entry.importance_score,
        "source_type": entry.source_type,
        "source_session_id": entry.source_session_id,
        "source_message_id": entry.source_message_id,
        "source_execution_id": entry.source_execution_id,
        "expires_at": entry.expires_at.isoformat() if entry.expires_at else None,
    }


def _entry_from_store_item(
    item: Any,
    *,
    tenant_id: str,
    user_id: str,
) -> MemoryEntry | None:
    value = item.value if isinstance(item.value, dict) else {}
    content = str(value.get("content") or "").strip()
    if not content:
        return None
    payload = value.get("payload")
    store_namespace = tuple(item.namespace)
    account_id = store_namespace[5] if len(store_namespace) > 5 else None
    return MemoryEntry(
        key=item.key,
        content=content,
        kind=str(value.get("kind") or "semantic"),
        payload=payload if isinstance(payload, dict) else {},
        tenant_id=tenant_id,
        user_id=user_id,
        account_id=account_id,
        memory_scope=MemoryScope.long_term,
        confidence=float(value.get("confidence") or 1.0),
        importance_score=float(value.get("importance_score") or 0.0),
        source_type=str(value.get("source_type") or ""),
        source_session_id=value.get("source_session_id"),
        source_message_id=value.get("source_message_id"),
        source_execution_id=value.get("source_execution_id"),
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
                item
                if isinstance(item, MemoryCandidate)
                else MemoryCandidate.model_validate(item)
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
        "preferences, goals, profile facts, or operating constraints worth remembering. "
        "Return only structured data matching the schema. Do not include secrets, passwords, "
        "API keys, bearer tokens, one-time codes, private credentials, or transient task details. "
        "Prefer concise Chinese memory statements when the source content is Chinese. "
        "Use confidence 0.0-1.0 and include only memories useful in future conversations.\n\n"
        f"Account name:\n{account_name}\n\n"
        f"Account positioning:\n{account_positioning[:1200]}\n\n"
        f"Existing memories:\n{existing_text}\n\n"
        f"User message:\n{user_message[:4000]}\n\n"
        f"Assistant response:\n{assistant_response[:6000]}\n\n"
        f"Tool results summary:\n{tool_text}"
    )
