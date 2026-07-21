from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from memory.types import MemoryEntry
from models.base import json_loads, utcnow
from models.enums import MemoryKind, MemorySourceType
from models.memory import MemoryRecord
from sqlalchemy import or_
from sqlmodel import Session, select


def normalize_memory_kind(kind: str | MemoryKind | None) -> MemoryKind:
    try:
        return MemoryKind(str(kind or MemoryKind.semantic))
    except ValueError:
        return MemoryKind.semantic


def normalize_memory_source_type(
    source_type: str | MemorySourceType | None,
) -> MemorySourceType:
    try:
        return MemorySourceType(str(source_type or MemorySourceType.manual))
    except ValueError:
        return MemorySourceType.manual


class MemoryRepository:
    def __init__(self, session: Session, *, auto_commit: bool = True) -> None:
        self.session = session
        self.auto_commit = auto_commit

    def upsert(
        self,
        key: str,
        *,
        content: str,
        user_id: str,
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
        self._validate_owner(agent_id=agent_id, session_id=session_id)
        row = self._find_active(
            key=key,
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
        )
        now = utcnow()
        if row is None:
            row = MemoryRecord(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
                memory_key=key,
            )
        else:
            row.version += 1
        row.user_id = user_id
        row.agent_id = agent_id
        row.session_id = session_id
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
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()
        self.session.refresh(row)
        return self._to_entry(row)

    def get(
        self,
        key: str,
        *,
        user_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
        touch: bool = True,
    ) -> MemoryEntry | None:
        query = self._scoped_query(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
        ).where(MemoryRecord.memory_key == key)
        row = self.session.exec(query).first()
        if row is None:
            return None
        if touch:
            self._record_access([row])
        return self._to_entry(row)

    def list_scope(
        self,
        *,
        user_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
        limit: int = 20,
        touch: bool = True,
    ) -> list[MemoryEntry]:
        rows = list(
            self.session.exec(
                self._scoped_query(
                    user_id=user_id,
                    agent_id=agent_id,
                    session_id=session_id,
                )
                .order_by(MemoryRecord.updated_at.desc())
                .limit(limit)
            ).all()
        )
        if touch:
            self._record_access(rows)
        return [self._to_entry(row) for row in rows]

    def search(
        self,
        query_text: str,
        *,
        user_id: str,
        agent_id: str | None = None,
        session_id: str | None = None,
        limit: int = 8,
        touch: bool = True,
    ) -> list[MemoryEntry]:
        candidates = self.list_scope(
            user_id=user_id,
            agent_id=agent_id,
            session_id=session_id,
            limit=100,
            touch=touch,
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
        user_id: str,
        agent_id: str | None,
        session_id: str | None,
    ) -> MemoryRecord | None:
        return self.session.exec(
            self._scoped_query(
                user_id=user_id,
                agent_id=agent_id,
                session_id=session_id,
            ).where(MemoryRecord.memory_key == key)
        ).first()

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
        user_id: str,
        agent_id: str | None,
        session_id: str | None,
    ):
        cls._validate_owner(agent_id=agent_id, session_id=session_id)
        query = cls._active_query().where(MemoryRecord.user_id == user_id)
        if agent_id is not None:
            return query.where(
                MemoryRecord.agent_id == agent_id,
                MemoryRecord.session_id.is_(None),
            )
        return query.where(
            MemoryRecord.session_id == session_id,
            MemoryRecord.agent_id.is_(None),
        )

    @staticmethod
    def _validate_owner(*, agent_id: str | None, session_id: str | None) -> None:
        if (agent_id is None) == (session_id is None):
            raise ValueError("exactly one of agent_id or session_id is required")

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
            user_id=row.user_id,
            agent_id=row.agent_id,
            session_id=row.session_id,
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


def _memory_query_terms(value: str) -> list[str]:
    text = str(value or "").casefold()
    terms = [item for item in re.findall(r"[a-z0-9_]{2,}", text) if item]
    for sequence in re.findall(r"[\u4e00-\u9fff]{2,}", text):
        terms.extend(sequence[index : index + 2] for index in range(len(sequence) - 1))
    return list(dict.fromkeys(terms))[:24]


def _clamp(value: float, *, minimum: float, maximum: float) -> float:
    return min(max(float(value), minimum), maximum)
