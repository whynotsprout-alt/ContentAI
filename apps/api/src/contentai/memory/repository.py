from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from sqlalchemy import String, and_, case, cast, func, literal, or_, update
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm.attributes import set_committed_value
from sqlmodel import Session, select

from contentai.memory.types import MemoryEntry
from contentai.models.base import json_loads, new_id, utcnow
from contentai.models.enums import MemoryKind, MemorySourceType
from contentai.models.memory import MemoryRecord


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
    def __init__(self, session: Session, *, auto_commit: bool = False) -> None:
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
        now = utcnow()
        values = {
            "id": new_id("mem"),
            "user_id": user_id,
            "agent_id": agent_id,
            "session_id": session_id,
            "memory_key": key,
            "kind": normalize_memory_kind(kind),
            "payload": payload or {},
            "content": content.strip(),
            "confidence": _normalize_unit_score(confidence, field_name="confidence"),
            "importance_score": _normalize_unit_score(
                importance_score,
                field_name="importance_score",
            ),
            "source_type": normalize_memory_source_type(source_type),
            "source_session_id": source_session_id,
            "source_message_id": source_message_id,
            "source_execution_id": source_execution_id,
            "version": 1,
            "access_count": 0,
            "last_accessed_at": None,
            "expires_at": expires_at,
            "deleted_at": None,
            "created_at": now,
            "updated_at": now,
        }
        statement = insert(MemoryRecord).values(**values)
        conflict_columns, conflict_predicate = self._conflict_target(
            agent_id=agent_id,
        )
        update_columns = {
            column_name: getattr(statement.excluded, column_name)
            for column_name in (
                "kind",
                "payload",
                "content",
                "confidence",
                "importance_score",
                "source_type",
                "source_session_id",
                "source_message_id",
                "source_execution_id",
                "expires_at",
                "deleted_at",
                "updated_at",
            )
        }
        update_columns["version"] = MemoryRecord.version + 1
        row = self.session.execute(
            statement.on_conflict_do_update(
                index_elements=conflict_columns,
                index_where=conflict_predicate,
                set_=update_columns,
            ).returning(MemoryRecord),
            execution_options={"populate_existing": True},
        ).scalar_one()
        entry = self._to_entry(row)
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()
        return entry

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
            return self._record_access([row])[row.id]
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
            touched = self._record_access(rows)
            return [touched[row.id] for row in rows]
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
        terms = _memory_query_terms(query_text)
        if not terms:
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
        else:
            # Rank across the complete active scope in PostgreSQL. Truncating
            # candidates before matching makes older exact memories
            # permanently unreachable once a scope grows past the cap.
            haystack = func.lower(
                func.concat(
                    cast(MemoryRecord.kind, String),
                    " ",
                    MemoryRecord.content,
                    " ",
                    cast(MemoryRecord.payload, String),
                )
            )
            matches = [haystack.contains(term, autoescape=True) for term in terms]
            score = sum(
                (case((term_match, 1), else_=0) for term_match in matches),
                start=literal(0),
            )
            rows = list(
                self.session.exec(
                    self._scoped_query(
                        user_id=user_id,
                        agent_id=agent_id,
                        session_id=session_id,
                    )
                    .where(or_(*matches))
                    .order_by(score.desc(), MemoryRecord.updated_at.desc())
                    .limit(limit)
                ).all()
            )

        # A search scans candidates, but only returned matches count as an
        # access. This keeps access statistics meaningful and avoids turning
        # read-only retrieval into a write-heavy operation.
        if touch:
            touched = self._record_access(rows)
            return [touched[row.id] for row in rows]
        return [self._to_entry(row) for row in rows]

    @staticmethod
    def _conflict_target(*, agent_id: str | None):
        if agent_id is not None:
            return (
                (MemoryRecord.user_id, MemoryRecord.agent_id, MemoryRecord.memory_key),
                and_(
                    MemoryRecord.agent_id.is_not(None),
                    MemoryRecord.deleted_at.is_(None),
                ),
            )
        return (
            (MemoryRecord.user_id, MemoryRecord.session_id, MemoryRecord.memory_key),
            and_(
                MemoryRecord.session_id.is_not(None),
                MemoryRecord.deleted_at.is_(None),
            ),
        )

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

    def _record_access(self, rows: list[MemoryRecord]) -> dict[str, MemoryEntry]:
        if not rows:
            return {}
        now = utcnow()
        updated_rows = self.session.execute(
            update(MemoryRecord)
            .where(MemoryRecord.id.in_([row.id for row in rows]))
            .values(
                access_count=MemoryRecord.access_count + 1,
                last_accessed_at=now,
            )
            .returning(
                MemoryRecord.id,
                MemoryRecord.access_count,
                MemoryRecord.last_accessed_at,
            ),
            execution_options={"synchronize_session": False},
        ).all()
        access_by_id = {
            row_id: (access_count, last_accessed_at)
            for row_id, access_count, last_accessed_at in updated_rows
        }
        for row in rows:
            access = access_by_id.get(row.id)
            if access is None:
                continue
            access_count, last_accessed_at = access
            set_committed_value(row, "access_count", access_count)
            set_committed_value(row, "last_accessed_at", last_accessed_at)
        entries = {row.id: self._to_entry(row) for row in rows}
        if self.auto_commit:
            self.session.commit()
        else:
            self.session.flush()
        return entries

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


def _normalize_unit_score(value: float, *, field_name: str) -> float:
    normalized = float(value)
    if not math.isfinite(normalized):
        raise ValueError(f"{field_name} must be finite")
    return min(max(normalized, 0.0), 1.0)
