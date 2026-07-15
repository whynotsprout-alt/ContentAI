from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from memory.types import MemoryEntry
from models.base import json_loads, utcnow
from models.enums import MemoryKind, MemoryOwnerType, MemoryScope, MemorySourceType
from models.memory import MemoryRecord
from sqlalchemy import or_
from sqlmodel import Session, select


def normalize_memory_kind(kind: str | MemoryKind | None) -> MemoryKind:
    try:
        return MemoryKind(str(kind or MemoryKind.semantic))
    except ValueError:
        return MemoryKind.semantic


def normalize_memory_owner_type(owner_type: str | MemoryOwnerType) -> MemoryOwnerType:
    try:
        return MemoryOwnerType(str(owner_type))
    except ValueError as exc:
        raise ValueError(f"Unsupported memory owner type: {owner_type}") from exc


def normalize_memory_source_type(
    source_type: str | MemorySourceType | None,
) -> MemorySourceType:
    try:
        return MemorySourceType(str(source_type or MemorySourceType.manual))
    except ValueError:
        return MemorySourceType.manual


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        key: str,
        *,
        content: str,
        tenant_id: str,
        user_id: str,
        owner_type: str | MemoryOwnerType,
        memory_scope: str | MemoryScope,
        kind: str | MemoryKind = MemoryKind.semantic,
        payload: dict[str, Any] | None = None,
        agent_id: str | None = None,
        session_id: str | None = None,
        confidence: float = 1.0,
        importance_score: float = 0.0,
        source_type: str | MemorySourceType | None = MemorySourceType.manual,
        source_session_id: str | None = None,
        source_message_id: str | None = None,
        source_execution_id: str | None = None,
        expires_at: datetime | None = None,
    ) -> MemoryEntry:
        scope = _normalize_scope(memory_scope)
        owner = normalize_memory_owner_type(owner_type)
        row = self._find_active(
            key=key,
            tenant_id=tenant_id,
            user_id=user_id,
            owner_type=owner,
            agent_id=agent_id,
            session_id=session_id,
            memory_scope=scope,
        )
        now = utcnow()
        if row is None:
            row = MemoryRecord(
                tenant_id=tenant_id,
                user_id=user_id,
                owner_type=owner,
                agent_id=agent_id,
                session_id=session_id,
                memory_scope=scope,
                memory_key=key,
            )
        else:
            row.version += 1
        row.tenant_id = tenant_id
        row.user_id = user_id
        row.owner_type = owner
        row.agent_id = agent_id
        row.session_id = session_id
        row.memory_scope = scope
        row.kind = normalize_memory_kind(kind)
        row.content = content.strip()
        row.payload = payload or {}
        row.confidence = _clamp(confidence, minimum=0.0, maximum=1.0)
        row.importance_score = max(float(importance_score), 0.0)
        row.source_type = normalize_memory_source_type(source_type)
        row.source_session_id = source_session_id
        row.source_message_id = source_message_id
        row.source_execution_id = source_execution_id
        row.expires_at = expires_at
        row.deleted_at = None
        row.touch_updated_at(now)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return self._to_entry(row)

    def get(
        self,
        key: str,
        *,
        tenant_id: str,
        user_id: str,
        owner_type: str | MemoryOwnerType,
        memory_scope: str | MemoryScope,
        agent_id: str | None = None,
        session_id: str | None = None,
    ) -> MemoryEntry | None:
        query = self._scoped_query(
            tenant_id=tenant_id,
            user_id=user_id,
            owner_type=owner_type,
            memory_scope=memory_scope,
            agent_id=agent_id,
            session_id=session_id,
        ).where(MemoryRecord.memory_key == key)
        row = self.session.exec(query).first()
        if row is None:
            return None
        self._record_access([row])
        return self._to_entry(row)

    def list_scope(
        self,
        *,
        tenant_id: str,
        user_id: str,
        owner_type: str | MemoryOwnerType,
        memory_scope: str | MemoryScope,
        agent_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
    ) -> list[MemoryEntry]:
        query = (
            self._scoped_query(
                tenant_id=tenant_id,
                user_id=user_id,
                owner_type=owner_type,
                memory_scope=memory_scope,
                agent_id=agent_id,
                session_id=session_id,
            )
            .order_by(MemoryRecord.updated_at.desc())
            .limit(limit)
        )
        rows = list(self.session.exec(query).all())
        self._record_access(rows)
        return [self._to_entry(row) for row in rows]

    def search(
        self,
        query_text: str,
        *,
        tenant_id: str,
        user_id: str,
        owner_type: str | MemoryOwnerType,
        memory_scope: str | MemoryScope,
        agent_id: str | None = None,
        session_id: str | None = None,
        limit: int = 8,
    ) -> list[MemoryEntry]:
        candidates = self.list_scope(
            tenant_id=tenant_id,
            user_id=user_id,
            owner_type=owner_type,
            agent_id=agent_id,
            session_id=session_id,
            memory_scope=memory_scope,
            limit=100,
        )
        terms = _memory_query_terms(query_text)
        if not terms:
            return candidates[:limit]

        scored: list[tuple[int, MemoryEntry]] = []
        for entry in candidates:
            haystack = f"{entry.kind} {entry.content} {entry.payload}".casefold()
            score = sum(1 for term in terms if term in haystack)
            if score:
                scored.append((score, entry))
        scored.sort(key=lambda item: (item[0], item[1].updated_at or utcnow()), reverse=True)
        return [entry for _, entry in scored[:limit]]

    def _find_active(
        self,
        *,
        key: str,
        tenant_id: str,
        user_id: str,
        owner_type: MemoryOwnerType,
        agent_id: str | None,
        session_id: str | None,
        memory_scope: MemoryScope,
    ) -> MemoryRecord | None:
        query = self._scoped_query(
            tenant_id=tenant_id,
            user_id=user_id,
            owner_type=owner_type,
            memory_scope=memory_scope,
            agent_id=agent_id,
            session_id=session_id,
        ).where(MemoryRecord.memory_key == key)
        return self.session.exec(query).first()

    @staticmethod
    def _active_query():
        now = utcnow()
        return select(MemoryRecord).where(
            MemoryRecord.deleted_at.is_(None),
            or_(MemoryRecord.expires_at.is_(None), MemoryRecord.expires_at > now),
        )

    @classmethod
    def _scoped_query(
        cls,
        *,
        tenant_id: str,
        user_id: str,
        owner_type: str | MemoryOwnerType,
        memory_scope: str | MemoryScope,
        agent_id: str | None,
        session_id: str | None,
    ):
        scope = _normalize_scope(memory_scope)
        owner = normalize_memory_owner_type(owner_type)
        query = cls._active_query().where(
            MemoryRecord.tenant_id == tenant_id,
            MemoryRecord.user_id == user_id,
            MemoryRecord.owner_type == owner,
            MemoryRecord.memory_scope == scope,
        )
        if owner == MemoryOwnerType.agent:
            query = query.where(MemoryRecord.agent_id == agent_id)
        if owner == MemoryOwnerType.session:
            query = query.where(MemoryRecord.session_id == session_id)
        return query

    def _record_access(self, rows: list[MemoryRecord]) -> None:
        if not rows:
            return
        now = utcnow()
        for row in rows:
            row.touch_accessed_at(now)
            self.session.add(row)
        self.session.commit()

    @staticmethod
    def _to_entry(row: MemoryRecord) -> MemoryEntry:
        payload = (
            row.payload if isinstance(row.payload, dict) else json_loads(row.payload, default={})
        )
        return MemoryEntry(
            key=row.memory_key,
            content=row.content,
            kind=str(row.kind),
            payload=payload if isinstance(payload, dict) else {},
            updated_at=row.updated_at,
            tenant_id=row.tenant_id,
            user_id=row.user_id,
            owner_type=row.owner_type,
            agent_id=row.agent_id,
            session_id=row.session_id,
            memory_scope=str(row.memory_scope),
            confidence=row.confidence,
            importance_score=row.importance_score,
            source_type=row.source_type,
            source_session_id=row.source_session_id,
            source_message_id=row.source_message_id,
            source_execution_id=row.source_execution_id,
            expires_at=row.expires_at,
            access_count=row.access_count,
            last_accessed_at=row.last_accessed_at,
        )


def _normalize_scope(scope: str | MemoryScope) -> MemoryScope:
    try:
        return MemoryScope(str(scope))
    except ValueError as exc:
        raise ValueError(f"Unsupported memory scope: {scope}") from exc


def _memory_query_terms(value: str) -> list[str]:
    """Use words plus CJK bigrams; never return broad recency results."""
    text = str(value or "").casefold()
    terms = [item for item in re.findall(r"[a-z0-9_]{2,}", text) if item]
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return list(dict.fromkeys(terms))[:24]


def _clamp(value: float, *, minimum: float, maximum: float) -> float:
    return min(max(float(value), minimum), maximum)
