from __future__ import annotations

from hashlib import sha1
from typing import Any

from agent.memory.repository import MemoryRepository
from agent.memory.retriever import extract_memory_candidates
from agent.memory.types import MemoryEntry


def long_term_namespace(account_id: str) -> tuple[str, ...]:
    return ("accounts", account_id, "long_term")


class LongTermMemory:
    def __init__(self, repository: MemoryRepository, store: Any) -> None:
        self.repository = repository
        self.store = store

    def remember(
        self,
        account_id: str,
        content: str,
        *,
        kind: str = "semantic",
        payload: dict[str, Any] | None = None,
        key: str | None = None,
    ) -> MemoryEntry:
        normalized = content.strip()
        if not normalized:
            raise ValueError("memory content cannot be empty")
        namespace = long_term_namespace(account_id)
        memory_key = key or self._stable_key(kind, normalized)
        entry = self.repository.upsert(
            namespace,
            memory_key,
            content=normalized[:1000],
            kind=kind,
            payload=payload or {},
        )
        self.store.put(
            namespace,
            memory_key,
            {"content": entry.content, "kind": entry.kind, "payload": entry.payload},
        )
        return entry

    def remember_from_user_message(self, account_id: str, message: str) -> list[MemoryEntry]:
        entries: list[MemoryEntry] = []
        for kind, content in extract_memory_candidates(message):
            entries.append(
                self.remember(
                    account_id,
                    content,
                    kind=kind,
                    payload={"source": "user_message"},
                )
            )
        return entries

    def recall(self, account_id: str, query: str, *, limit: int = 8) -> list[MemoryEntry]:
        namespace = long_term_namespace(account_id)
        self._bootstrap_store(namespace)
        db_entries = self.repository.search(namespace, query, limit=limit)
        seen = {entry.key for entry in db_entries}
        for item in self.store.search(namespace, query=query, limit=limit):
            if item.key in seen:
                continue
            value = item.value if isinstance(item.value, dict) else {}
            content = str(value.get("content") or "").strip()
            if not content:
                continue
            db_entries.append(
                MemoryEntry(
                    key=item.key,
                    namespace=tuple(item.namespace),
                    content=content,
                    kind=str(value.get("kind") or "semantic"),
                    payload=value.get("payload") if isinstance(value.get("payload"), dict) else {},
                )
            )
            seen.add(item.key)
        return db_entries[:limit]

    def list_all(self, account_id: str, *, limit: int = 20) -> list[MemoryEntry]:
        namespace = long_term_namespace(account_id)
        self._bootstrap_store(namespace)
        return self.repository.list_namespace(namespace, limit=limit)

    def _bootstrap_store(self, namespace: tuple[str, ...]) -> None:
        for entry in self.repository.list_namespace(namespace, limit=100):
            if self.store.get(namespace, entry.key) is None:
                self.store.put(
                    namespace,
                    entry.key,
                    {"content": entry.content, "kind": entry.kind, "payload": entry.payload},
                )

    @staticmethod
    def _stable_key(kind: str, content: str) -> str:
        digest = sha1(f"{kind}:{content}".encode()).hexdigest()[:16]
        return f"{kind}_{digest}"
