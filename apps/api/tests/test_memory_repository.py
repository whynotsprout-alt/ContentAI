import math
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier, Lock
from typing import Any

import pytest
from contentai.db.session import get_engine
from contentai.memory import LongTermMemory, MemoryRepository
from contentai.models.base import utcnow
from contentai.models.chat import ChatSession
from contentai.models.memory import MemoryRecord
from sqlalchemy import text
from sqlmodel import Session, select


def _session(session: Session, session_id: str) -> ChatSession:
    chat = ChatSession(
        id=session_id,
        agent_id="default-agent",
        agent_version_id="default-agent-v1",
        user_id="local-user",
    )
    session.add(chat)
    session.commit()
    return chat


def test_memory_repository_allows_large_content():
    large_content = "summary line " * 600
    with Session(get_engine()) as session:
        chat = _session(session, "large-content-test")
        entry = MemoryRepository(session).upsert(
            "summary",
            content=large_content,
            kind="summary",
            user_id="local-user",
            session_id=chat.id,
        )
    assert entry.content == large_content.strip()
    assert entry.memory_scope == "short_term"


@pytest.mark.parametrize(
    ("field_name", "value"),
    [
        ("confidence", math.nan),
        ("confidence", math.inf),
        ("confidence", -math.inf),
        ("importance_score", math.nan),
        ("importance_score", math.inf),
        ("importance_score", -math.inf),
    ],
)
def test_memory_repository_rejects_nonfinite_scores(
    field_name: str,
    value: float,
) -> None:
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        with pytest.raises(ValueError, match=rf"^{field_name} must be finite$"):
            repository.upsert(
                "nonfinite-score",
                content="must not be stored",
                user_id="local-user",
                agent_id="default-agent",
                **{field_name: value},
            )
        stored = session.exec(
            select(MemoryRecord).where(MemoryRecord.memory_key == "nonfinite-score")
        ).first()

    assert stored is None


def test_memory_repository_clamps_finite_scores_to_unit_interval() -> None:
    with Session(get_engine()) as session:
        entry = MemoryRepository(session).upsert(
            "bounded-scores",
            content="finite scores are normalized",
            user_id="local-user",
            agent_id="default-agent",
            confidence=-0.5,
            importance_score=1.5,
        )

    assert entry.confidence == 0.0
    assert entry.importance_score == 1.0


def test_memory_repository_isolates_long_term_and_session_records():
    with Session(get_engine()) as session:
        chat = _session(session, "scope-test")
        repository = MemoryRepository(session)
        repository.upsert(
            "preference",
            content="durable preference",
            user_id="local-user",
            agent_id="default-agent",
        )
        repository.upsert(
            "preference",
            content="session summary",
            user_id="local-user",
            session_id=chat.id,
        )
        durable = repository.search(
            "preference", user_id="local-user", agent_id="default-agent"
        )
        transient = repository.list_scope(user_id="local-user", session_id=chat.id)
    assert [item.content for item in durable] == ["durable preference"]
    assert [item.content for item in transient] == ["session summary"]


def test_memory_repository_filters_expired_and_deleted_records():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        repository.upsert(
            "active",
            content="active memory",
            user_id="local-user",
            agent_id="default-agent",
        )
        repository.upsert(
            "expired",
            content="expired memory",
            user_id="local-user",
            agent_id="default-agent",
            expires_at=utcnow() - timedelta(days=1),
        )
        deleted = repository.upsert(
            "deleted",
            content="deleted memory",
            user_id="local-user",
            agent_id="default-agent",
        )
        row = session.exec(select(MemoryRecord).where(MemoryRecord.memory_key == deleted.key)).one()
        row.deleted_at = utcnow()
        session.add(row)
        session.commit()
        listed = repository.list_scope(user_id="local-user", agent_id="default-agent")
    assert [item.content for item in listed] == ["active memory"]


def test_memory_repository_reuses_expired_record_on_upsert():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        original = repository.upsert(
            "rotating",
            content="old memory",
            user_id="local-user",
            agent_id="default-agent",
            expires_at=utcnow() - timedelta(days=1),
        )
        original_row = session.exec(
            select(MemoryRecord).where(MemoryRecord.memory_key == original.key)
        ).one()
        refreshed = repository.upsert(
            "rotating",
            content="new memory",
            user_id="local-user",
            agent_id="default-agent",
        )
        rows = session.exec(
            select(MemoryRecord).where(
                MemoryRecord.user_id == "local-user",
                MemoryRecord.agent_id == "default-agent",
                MemoryRecord.memory_key == "rotating",
            )
        ).all()

    assert refreshed.content == "new memory"
    assert refreshed.expires_at is None
    assert len(rows) == 1
    assert rows[0].id == original_row.id


def test_memory_repository_concurrent_upserts_keep_one_active_row() -> None:
    barrier = Barrier(2)

    def upsert(content: str) -> str:
        with Session(get_engine()) as session:
            barrier.wait(timeout=10)
            entry = MemoryRepository(session).upsert(
                "concurrent",
                content=content,
                user_id="local-user",
                agent_id="default-agent",
            )
            session.commit()
            return entry.content

    with ThreadPoolExecutor(max_workers=2) as executor:
        contents = list(executor.map(upsert, ("first value", "second value")))

    with Session(get_engine()) as session:
        rows = session.exec(
            select(MemoryRecord).where(
                MemoryRecord.user_id == "local-user",
                MemoryRecord.agent_id == "default-agent",
                MemoryRecord.memory_key == "concurrent",
                MemoryRecord.deleted_at.is_(None),
            )
        ).all()

    assert set(contents) == {"first value", "second value"}
    assert len(rows) == 1
    assert rows[0].version == 2
    assert rows[0].content in contents


def test_concurrent_memory_batches_upsert_in_the_same_stable_key_order() -> None:
    candidate_a = {
        "kind": "preference",
        "content": "Alpha durable preference",
        "confidence": 0.95,
        "importance_score": 0.8,
        "reason": "alpha",
    }
    candidate_b = {
        "kind": "preference",
        "content": "Beta durable preference",
        "confidence": 0.95,
        "importance_score": 0.8,
        "reason": "beta",
    }
    expected_order = sorted(
        [
            LongTermMemory._stable_key("preference", candidate_a["content"]),
            LongTermMemory._stable_key("preference", candidate_b["content"]),
        ]
    )
    first_key_barrier = Barrier(2)
    distinct_first_locks_barrier = Barrier(2)
    first_keys_lock = Lock()
    first_keys: dict[str, str] = {}
    actual_orders: dict[str, list[str]] = {"forward": [], "reverse": []}

    class BatchModel:
        def __init__(self, memories: list[dict[str, Any]]) -> None:
            self.memories = memories

        def invoke(self, _prompt: str, **_kwargs: Any) -> dict[str, Any]:
            return {"memories": self.memories}

    class BatchGateway:
        def __init__(self, memories: list[dict[str, Any]]) -> None:
            self.memories = memories

        def build_structured_output_model(self, _schema: Any) -> BatchModel:
            return BatchModel(self.memories)

    class CoordinatedRepository(MemoryRepository):
        def __init__(self, session: Session, worker_id: str) -> None:
            super().__init__(session, auto_commit=False)
            self.worker_id = worker_id

        def upsert(self, key: str, **kwargs: Any):
            position = len(actual_orders[self.worker_id])
            actual_orders[self.worker_id].append(key)
            if position == 0:
                with first_keys_lock:
                    first_keys[self.worker_id] = key
                first_key_barrier.wait(timeout=10)

            entry = super().upsert(key, **kwargs)

            if position == 0:
                with first_keys_lock:
                    first_keys_are_distinct = len(set(first_keys.values())) == 2
                # This second rendezvous is enabled only for the unsafe order.
                # It makes opposite first-row locks deterministically deadlock,
                # while the stable same-key order never waits behind its peer.
                if first_keys_are_distinct:
                    distinct_first_locks_barrier.wait(timeout=10)
            return entry

    def persist_batch(worker_id: str, memories: list[dict[str, Any]]) -> list[str]:
        with Session(get_engine()) as session:
            session.exec(text("SET LOCAL lock_timeout = '5s'"))
            memory = LongTermMemory(CoordinatedRepository(session, worker_id))
            memory.remember_after_turn(
                agent_id="default-agent",
                account_name="Default Agent",
                account_positioning="Test account",
                user_message="Remember durable preferences.",
                assistant_response="Acknowledged.",
                tool_results=[],
                model_gateway=BatchGateway(memories),
                user_id="local-user",
                existing_memories=[],
            )
            session.commit()
        return actual_orders[worker_id]

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [
            executor.submit(persist_batch, "forward", [candidate_a, candidate_b]),
            executor.submit(persist_batch, "reverse", [candidate_b, candidate_a]),
        ]
        completed_orders = [future.result(timeout=15) for future in futures]

    assert completed_orders == [expected_order, expected_order]
    with Session(get_engine()) as session:
        rows = session.exec(
            select(MemoryRecord).where(
                MemoryRecord.user_id == "local-user",
                MemoryRecord.agent_id == "default-agent",
                MemoryRecord.memory_key.in_(expected_order),
            )
        ).all()
    assert {row.memory_key for row in rows} == set(expected_order)
    assert {row.version for row in rows} == {2}


def test_memory_repository_concurrent_reads_increment_access_count_atomically() -> None:
    with Session(get_engine()) as session:
        MemoryRepository(session).upsert(
            "concurrent-access",
            content="read by two workers",
            user_id="local-user",
            agent_id="default-agent",
        )
        session.commit()
        original = session.exec(
            select(MemoryRecord).where(MemoryRecord.memory_key == "concurrent-access")
        ).one()
        original_updated_at = original.updated_at

    barrier = Barrier(2)

    def read() -> int:
        with Session(get_engine()) as session:
            barrier.wait(timeout=10)
            entry = MemoryRepository(session).get(
                "concurrent-access",
                user_id="local-user",
                agent_id="default-agent",
            )
            assert entry is not None
            session.commit()
            return entry.access_count

    with ThreadPoolExecutor(max_workers=2) as executor:
        observed_counts = list(executor.map(lambda _index: read(), range(2)))

    with Session(get_engine()) as session:
        row = session.exec(
            select(MemoryRecord).where(MemoryRecord.memory_key == "concurrent-access")
        ).one()

    assert set(observed_counts) == {1, 2}
    assert row.access_count == 2
    assert row.updated_at == original_updated_at


def test_memory_repository_does_not_commit_access_updates_by_default():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        repository.upsert(
            "transactional",
            content="transactional memory",
            user_id="local-user",
            agent_id="default-agent",
        )
        session.commit()
        repository.get("transactional", user_id="local-user", agent_id="default-agent")
        session.rollback()

    with Session(get_engine()) as session:
        row = session.exec(
            select(MemoryRecord).where(MemoryRecord.memory_key == "transactional")
        ).one()
        assert row.access_count == 0


def test_memory_repository_search_touches_only_returned_matches():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        for key, content in (
            ("search-a", "alpha preference"),
            ("search-b", "beta preference"),
            ("search-c", "gamma preference"),
        ):
            repository.upsert(
                key,
                content=content,
                user_id="local-user",
                agent_id="default-agent",
            )

        results = repository.search(
            "preference",
            user_id="local-user",
            agent_id="default-agent",
            limit=1,
        )
        rows = session.exec(
            select(MemoryRecord).where(
                MemoryRecord.user_id == "local-user",
                MemoryRecord.agent_id == "default-agent",
            )
        ).all()

    assert len(results) == 1
    assert sum(row.access_count for row in rows) == 1


def test_memory_repository_search_reaches_matches_older_than_one_hundred_rows():
    now = utcnow()
    target = MemoryRecord(
        user_id="local-user",
        agent_id="default-agent",
        memory_key="old-exact-match",
        content="unique archaeological signal",
        updated_at=now - timedelta(days=1),
    )
    decoys = [
        MemoryRecord(
            user_id="local-user",
            agent_id="default-agent",
            memory_key=f"new-decoy-{index}",
            content=f"recent unrelated memory {index}",
            updated_at=now + timedelta(seconds=index),
        )
        for index in range(100)
    ]
    with Session(get_engine()) as session:
        session.add(target)
        session.add_all(decoys)
        session.commit()

        results = MemoryRepository(session).search(
            "archaeological signal",
            user_id="local-user",
            agent_id="default-agent",
            limit=1,
        )

    assert [entry.key for entry in results] == ["old-exact-match"]
    assert results[0].access_count == 1


def test_memory_repository_tracks_access_statistics():
    with Session(get_engine()) as session:
        repository = MemoryRepository(session)
        repository.upsert(
            "remembered",
            content="accessed memory",
            user_id="local-user",
            agent_id="default-agent",
        )
        entry = repository.get(
            "remembered",
            user_id="local-user",
            agent_id="default-agent",
        )
    assert entry is not None
    assert entry.access_count == 1
    assert entry.last_accessed_at is not None
