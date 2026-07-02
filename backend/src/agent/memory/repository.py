from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from agent.memory.types import MemoryEntry
from models.base import json_dumps, json_loads, utcnow
from models.memory import MemoryRecord
from sqlmodel import Session, select


def encode_namespace(namespace: Iterable[str]) -> str:
    return json_dumps(list(namespace))


def decode_namespace(value: str) -> tuple[str, ...]:
    raw = json_loads(value, default=[])
    if not isinstance(raw, list):
        return ()
    return tuple(str(item) for item in raw if str(item).strip())


class MemoryRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def upsert(
        self,
        namespace: tuple[str, ...],
        key: str,
        *,
        content: str,
        kind: str = "semantic",
        payload: dict[str, Any] | None = None,
    ) -> MemoryEntry:
        encoded_namespace = encode_namespace(namespace)
        row = self.session.exec(
            select(MemoryRecord).where(
                MemoryRecord.namespace == encoded_namespace,
                MemoryRecord.memory_key == key,
            )
        ).first()
        now = utcnow()
        if row is None:
            row = MemoryRecord(namespace=encoded_namespace, memory_key=key)
        row.kind = kind
        row.content = content.strip()
        row.payload = json_dumps(payload or {})
        row.touch_updated_at(now)
        self.session.add(row)
        self.session.commit()
        self.session.refresh(row)
        return self._to_entry(row)

    def get(self, namespace: tuple[str, ...], key: str) -> MemoryEntry | None:
        row = self.session.exec(
            select(MemoryRecord).where(
                MemoryRecord.namespace == encode_namespace(namespace),
                MemoryRecord.memory_key == key,
            )
        ).first()
        return self._to_entry(row) if row is not None else None

    def list_namespace(self, namespace: tuple[str, ...], *, limit: int = 20) -> list[MemoryEntry]:
        rows = self.session.exec(
            select(MemoryRecord)
            .where(MemoryRecord.namespace == encode_namespace(namespace))
            .order_by(MemoryRecord.updated_at.desc())
            .limit(limit)
        ).all()
        return [self._to_entry(row) for row in rows]

    def search(
        self,
        namespace: tuple[str, ...],
        query: str,
        *,
        limit: int = 8,
    ) -> list[MemoryEntry]:
        candidates = self.list_namespace(namespace, limit=100)
        terms = [term.casefold() for term in query.split() if term.strip()]
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

    @staticmethod
    def _to_entry(row: MemoryRecord) -> MemoryEntry:
        payload = json_loads(row.payload, default={})
        return MemoryEntry(
            key=row.memory_key,
            namespace=decode_namespace(row.namespace),
            content=row.content,
            kind=row.kind,
            payload=payload if isinstance(payload, dict) else {},
            updated_at=row.updated_at,
        )
