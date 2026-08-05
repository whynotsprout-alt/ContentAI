from __future__ import annotations

from typing import Any

import pytest
from contentai.agent.runtime import execution_services
from contentai.agent.runtime.execution_services import (
    _extract_memory_background,
    _refresh_short_summary_background,
)
from contentai.core.config import get_settings
from contentai.db.session import get_engine
from contentai.memory import LongTermMemory, MemoryRepository
from contentai.memory.types import MemoryEntry
from contentai.models.chat import ChatMessage, ChatSession
from contentai.models.enums import MessageRole
from contentai.models.memory import MemoryRecord
from sqlmodel import Session, select


def _chat(session_id: str) -> ChatSession:
    return ChatSession(
        id=session_id,
        agent_id="default-agent",
        agent_version_id="default-agent-v1",
        user_id="local-user",
    )


def test_default_upsert_does_not_commit_the_callers_unit_of_work() -> None:
    chat_id = "memory-uow-upsert-chat"
    memory_key = "memory-uow-upsert"

    with Session(get_engine()) as session:
        session.add(_chat(chat_id))
        MemoryRepository(session).upsert(
            memory_key,
            content="rollback this memory with the surrounding work",
            user_id="local-user",
            agent_id="default-agent",
        )
        session.rollback()

    with Session(get_engine()) as session:
        assert session.get(ChatSession, chat_id) is None
        assert session.exec(
            select(MemoryRecord).where(MemoryRecord.memory_key == memory_key)
        ).first() is None


def test_default_touch_does_not_commit_the_callers_unit_of_work() -> None:
    chat_id = "memory-uow-touch-chat"
    memory_key = "memory-uow-touch"

    with Session(get_engine()) as session:
        MemoryRepository(session).upsert(
            memory_key,
            content="persisted seed",
            user_id="local-user",
            agent_id="default-agent",
        )
        session.commit()

    with Session(get_engine()) as session:
        session.add(_chat(chat_id))
        touched = MemoryRepository(session).get(
            memory_key,
            user_id="local-user",
            agent_id="default-agent",
        )
        assert touched is not None
        assert touched.access_count == 1
        session.rollback()

    with Session(get_engine()) as session:
        assert session.get(ChatSession, chat_id) is None
        row = session.exec(
            select(MemoryRecord).where(MemoryRecord.memory_key == memory_key)
        ).one()
        assert row.access_count == 0
        assert row.last_accessed_at is None


def test_owned_short_summary_workflow_commits_once_complete() -> None:
    chat_id = "memory-owned-summary-chat"
    with Session(get_engine()) as session:
        session.add(_chat(chat_id))
        session.flush()
        session.add(
            ChatMessage(
                id="memory-owned-summary-message",
                session_id=chat_id,
                role=MessageRole.user,
                content="Durable constraint: cite every source.",
            )
        )
        session.commit()

    _refresh_short_summary_background(
        session_id=chat_id,
        settings=get_settings(),
    )

    with Session(get_engine()) as session:
        row = session.exec(
            select(MemoryRecord).where(
                MemoryRecord.session_id == chat_id,
                MemoryRecord.memory_key == "summary",
            )
        ).one()
        assert "cite every source" in row.content


class _NoopEventWriter:
    def __init__(self, *_args: Any, **_kwargs: Any) -> None:
        pass

    def emit(self, _event: str, _payload: dict[str, Any]) -> None:
        pass

    def close(self) -> None:
        pass


class _MemoryExtractionModel:
    def __init__(
        self,
        *,
        memories: list[dict[str, Any]] | None = None,
        on_invoke: Any | None = None,
    ) -> None:
        self.memories = memories
        self.on_invoke = on_invoke

    def invoke(self, _prompt: str, **_kwargs: Any) -> dict[str, list[dict[str, Any]]]:
        if self.on_invoke is not None:
            self.on_invoke()
        return {
            "memories": self.memories
            if self.memories is not None
            else [
                {
                    "kind": "preference",
                    "content": "prefers transactionally persisted memories",
                    "confidence": 0.95,
                    "importance_score": 0.8,
                    "reason": "stable preference",
                }
            ]
        }


class _MemoryExtractionGateway:
    def __init__(
        self,
        *,
        memories: list[dict[str, Any]] | None = None,
        on_invoke: Any | None = None,
    ) -> None:
        self.memories = memories
        self.on_invoke = on_invoke

    def build_structured_output_model(self, _schema: Any) -> _MemoryExtractionModel:
        return _MemoryExtractionModel(
            memories=self.memories,
            on_invoke=self.on_invoke,
        )


def _run_memory_extraction(model_gateway: Any | None = None) -> None:
    _extract_memory_background(
        execution_id="memory-owned-extraction",
        agent_id="default-agent",
        user_id="local-user",
        session_id="memory-owned-conversation",
        account_name="Default Agent",
        account_positioning="Test account",
        user_message="Remember my durable preference.",
        source_message_id="memory-owned-source-message",
        assistant_text="I will remember it.",
        tool_results=[],
        model_gateway=model_gateway or _MemoryExtractionGateway(),
        settings=get_settings(),
    )


def test_owned_memory_extraction_commits_once_complete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execution_services,
        "PersistentAgentEventWriter",
        _NoopEventWriter,
    )

    _run_memory_extraction()

    with Session(get_engine()) as session:
        row = session.exec(
            select(MemoryRecord).where(
                MemoryRecord.content == "prefers transactionally persisted memories"
            )
        ).one()
        assert row.user_id == "local-user"
        assert row.agent_id == "default-agent"


def test_memory_extraction_releases_read_transaction_before_model_invoke(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execution_services,
        "PersistentAgentEventWriter",
        _NoopEventWriter,
    )
    repository_sessions: list[Session] = []
    repository_type = MemoryRepository

    def tracked_repository(
        session: Session,
        *,
        auto_commit: bool = False,
    ) -> MemoryRepository:
        repository_sessions.append(session)
        return repository_type(session, auto_commit=auto_commit)

    monkeypatch.setattr(execution_services, "MemoryRepository", tracked_repository)
    observed_model_boundary: list[bool] = []

    def assert_model_boundary() -> None:
        assert len(repository_sessions) == 2
        read_session, write_session = repository_sessions
        assert read_session.get_transaction() is None
        assert not read_session.in_transaction()
        assert write_session.get_transaction() is None
        assert not write_session.in_transaction()
        observed_model_boundary.append(True)

    _run_memory_extraction(
        _MemoryExtractionGateway(on_invoke=assert_model_boundary)
    )

    assert observed_model_boundary == [True]
    assert all(not session.in_transaction() for session in repository_sessions)


class _RecordingMemoryRepository:
    def __init__(self) -> None:
        self.upserts: list[dict[str, Any]] = []

    def list_scope(self, **_kwargs: Any) -> list[MemoryEntry]:
        raise AssertionError("existing_memories must bypass the repository fallback read")

    def upsert(self, key: str, **kwargs: Any) -> MemoryEntry:
        self.upserts.append({"key": key, **kwargs})
        return MemoryEntry(
            key=key,
            content=kwargs["content"],
            user_id=kwargs["user_id"],
            kind=str(kwargs["kind"]),
            payload=kwargs["payload"],
            agent_id=kwargs["agent_id"],
        )


def test_memory_candidates_are_normalized_deduplicated_and_key_sorted() -> None:
    repository = _RecordingMemoryRepository()
    memory = LongTermMemory(repository)  # type: ignore[arg-type]
    candidates = [
        {
            "kind": "preference",
            "content": " Beta durable preference ",
            "confidence": 0.91,
            "importance_score": 0.7,
            "reason": "beta",
        },
        {
            "kind": "not-a-memory-kind",
            "content": " Alpha durable fact ",
            "confidence": 0.92,
            "importance_score": 0.8,
            "reason": "alpha normalized",
        },
        {
            "kind": "semantic",
            "content": "Alpha durable fact",
            "confidence": 0.99,
            "importance_score": 0.9,
            "reason": "duplicate",
        },
        {
            "kind": "goal",
            "content": "ignored for low confidence",
            "confidence": 0.2,
            "importance_score": 0.9,
            "reason": "low confidence",
        },
        {
            "kind": "semantic",
            "content": "API key must never be stored",
            "confidence": 0.99,
            "importance_score": 0.9,
            "reason": "sensitive",
        },
        {
            "kind": "semantic",
            "content": "这次任务已完成口播稿",
            "confidence": 0.99,
            "importance_score": 0.9,
            "reason": "transient",
        },
    ]

    entries = memory.remember_after_turn(
        agent_id="default-agent",
        account_name="Default Agent",
        account_positioning="Test account",
        user_message="Remember durable facts.",
        assistant_response="Acknowledged.",
        tool_results=[],
        model_gateway=_MemoryExtractionGateway(memories=candidates),
        user_id="local-user",
        existing_memories=[],
    )

    expected_keys = sorted(
        [
            LongTermMemory._stable_key("semantic", "Alpha durable fact"),
            LongTermMemory._stable_key("preference", "Beta durable preference"),
        ]
    )
    assert [upsert["key"] for upsert in repository.upserts] == expected_keys
    assert [entry.key for entry in entries] == expected_keys
    assert {upsert["content"] for upsert in repository.upserts} == {
        "Alpha durable fact",
        "Beta durable preference",
    }
    assert {str(upsert["kind"]) for upsert in repository.upserts} == {
        "semantic",
        "preference",
    }


def test_memory_extraction_does_not_retry_internal_type_error() -> None:
    class InternalTypeErrorModel:
        def __init__(self) -> None:
            self.calls = 0

        def invoke(self, _prompt: str, **_kwargs: Any) -> Any:
            self.calls += 1
            raise TypeError("provider config processing failed internally")

    class InternalTypeErrorGateway:
        def __init__(self, model: InternalTypeErrorModel) -> None:
            self.model = model

        def build_structured_output_model(self, _schema: Any) -> InternalTypeErrorModel:
            return self.model

    model = InternalTypeErrorModel()
    memory = LongTermMemory(_RecordingMemoryRepository())  # type: ignore[arg-type]

    with pytest.raises(TypeError, match="config processing failed internally"):
        memory.remember_after_turn(
            agent_id="default-agent",
            account_name="Default Agent",
            account_positioning="Test account",
            user_message="Remember durable facts.",
            assistant_response="Acknowledged.",
            tool_results=[],
            model_gateway=InternalTypeErrorGateway(model),
            user_id="local-user",
            existing_memories=[],
        )

    assert model.calls == 1


def test_owned_memory_extraction_rolls_back_partial_work_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        execution_services,
        "PersistentAgentEventWriter",
        _NoopEventWriter,
    )

    def remember_then_fail(self: LongTermMemory, **_kwargs: Any) -> list[Any]:
        self.remember(
            "default-agent",
            "partial memory must be rolled back",
            user_id="local-user",
        )
        raise RuntimeError("forced extraction failure")

    monkeypatch.setattr(LongTermMemory, "remember_after_turn", remember_then_fail)

    with pytest.raises(RuntimeError, match="forced extraction failure"):
        _run_memory_extraction()

    with Session(get_engine()) as session:
        assert session.exec(
            select(MemoryRecord).where(
                MemoryRecord.content == "partial memory must be rolled back"
            )
        ).first() is None
