from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import agent.runtime.events as runtime_events
from agent.runtime.events import AgentEventWriter, PersistentAgentEventWriter
from db.session import get_engine
from models.chat import AgentExecution, AgentInvocation, ChatSession
from services.event_stream import (
    RedisEventStream,
    StreamEvent,
    execution_stream_key,
    redis_stream_id,
)
from sqlalchemy import inspect
from sqlmodel import Session


class FakePipeline:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def xadd(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("xadd", args, kwargs))

    def expire(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("expire", args, kwargs))

    def execute(self) -> list[Any]:
        return [True] * len(self.commands)


class CapturingEventWriter(AgentEventWriter):
    def __init__(self, settings: Any) -> None:
        super().__init__("execution-batched", settings=settings)
        self.events: list[tuple[str, dict[str, Any]]] = []

    def _write_event(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))


class FakeRedis:
    def __init__(self) -> None:
        self.last_pipeline: FakePipeline | None = None
        self.read_response: list[Any] = []
        self.read_args: tuple[Any, ...] | None = None
        self.read_kwargs: dict[str, Any] | None = None

    def pipeline(self, *, transaction: bool) -> FakePipeline:
        assert transaction is False
        self.last_pipeline = FakePipeline()
        return self.last_pipeline

    def xread(self, *args: Any, **kwargs: Any) -> list[Any]:
        self.read_args = args
        self.read_kwargs = kwargs
        return self.read_response


def test_publish_uses_event_sequence_as_stream_id() -> None:
    redis = FakeRedis()
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)
    created_at = datetime(2026, 7, 13, 8, 0, tzinfo=UTC)

    stream.publish(
        [
            StreamEvent(
                execution_id="exe-1",
                event_type="token",
                sequence=7,
                payload={"content": "你好"},
                timestamp=created_at,
            )
        ]
    )

    assert redis.last_pipeline is not None
    xadd = redis.last_pipeline.commands[0]
    assert xadd[0] == "xadd"
    assert xadd[1][0] == execution_stream_key("exe-1")
    assert xadd[2]["id"] == "7-0"
    assert xadd[2]["maxlen"] == 100
    assert xadd[1][1]["sequence"] == "7"
    assert xadd[1][1]["type"] == "token"
    assert redis.last_pipeline.commands[-1] == (
        "expire",
        (execution_stream_key("exe-1"), 60),
        {},
    )


def test_read_starts_after_database_sequence_and_decodes_payload() -> None:
    redis = FakeRedis()
    redis.read_response = [
        (
            execution_stream_key("exe-1"),
            [
                (
                    "8-0",
                    {
                        "sequence": "8",
                        "execution_id": "exe-1",
                        "type": "done",
                        "timestamp": "2026-07-13T08:01:00+00:00",
                        "data": '{"status":"completed"}',
                    },
                )
            ],
        )
    ]
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100, block_ms=1234)

    events = stream.read("exe-1", after_sequence=7)

    assert redis.read_args == ({execution_stream_key("exe-1"): redis_stream_id(7)},)
    assert redis.read_kwargs == {"block": 1234}
    assert len(events) == 1
    assert events[0].sequence == 8
    assert events[0].event_type == "done"
    assert events[0].payload == {"status": "completed"}


def test_runtime_writer_does_not_persist_intermediate_events() -> None:
    with Session(get_engine()) as session:
        chat = ChatSession(
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id="default-agent-v1",
        )
        session.add(execution)
        session.commit()
        execution_id = execution.id

    class CapturingPublisher:
        def __init__(self) -> None:
            self.sequences: list[int] = []

        def publish(self, events: list[object]) -> None:
            self.sequences.extend(int(event.sequence) for event in events)

    publisher = CapturingPublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
    )
    writer.emit("state", {"status": "running"})
    writer.close()

    assert publisher.sequences == [1]
    assert "agentevent" not in inspect(get_engine()).get_table_names()


def test_assistant_stream_chunks_batch_at_256_characters(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(runtime_events, "monotonic", lambda: clock[0], raising=False)
    writer = CapturingEventWriter(
        SimpleNamespace(
            agent=SimpleNamespace(event_flush_interval_ms=50, event_flush_max_chars=256)
        )
    )

    for chunk in ("a" * 100, "b" * 100):
        writer.emit(
            "assistant_message_delta",
            {"message_id": "message-1", "chunk": chunk, "done": False},
        )
    assert writer.events == []

    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": "c" * 56, "done": False},
    )

    assert len(writer.events) == 1
    assert writer.events[0][0] == "token"
    assert writer.events[0][1]["content"] == "a" * 100 + "b" * 100 + "c" * 56


def test_assistant_stream_chunks_batch_at_50_milliseconds(monkeypatch):
    clock = [0.0]
    monkeypatch.setattr(runtime_events, "monotonic", lambda: clock[0], raising=False)
    writer = CapturingEventWriter(
        SimpleNamespace(
            agent=SimpleNamespace(event_flush_interval_ms=50, event_flush_max_chars=256)
        )
    )
    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": "first", "done": False},
    )
    clock[0] = 0.051
    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": " second", "done": False},
    )

    assert [payload["content"] for _event, payload in writer.events] == ["first second"]


def test_terminal_events_flush_pending_assistant_chunks(monkeypatch):
    monkeypatch.setattr(runtime_events, "monotonic", lambda: 0.0, raising=False)
    settings = SimpleNamespace(
        agent=SimpleNamespace(event_flush_interval_ms=50, event_flush_max_chars=256)
    )

    for terminal_event in (
        "execution_completed",
        "execution_failed",
        "execution_cancelled",
        "execution_waiting_input",
    ):
        writer = CapturingEventWriter(settings)
        writer.emit(
            "assistant_message_delta",
            {"message_id": "message-1", "chunk": "tail", "done": False},
        )
        assert writer.events == []
        writer.emit(terminal_event, {"status": terminal_event})

        assert writer.events[0][0] == "token"
        assert writer.events[0][1]["content"] == "tail"
        assert len(writer.events) == 2
