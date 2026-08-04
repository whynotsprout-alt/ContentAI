from __future__ import annotations

import threading
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import contentai.agent.runtime.events as runtime_events
from contentai.agent.runtime.events import AgentEventWriter, PersistentAgentEventWriter
from contentai.db.session import get_engine
from contentai.models.chat import AgentExecution, AgentInvocation, ChatSession
from contentai.services.event_stream import (
    RedisEventStream,
    StreamEvent,
    execution_stream_key,
    redis_stream_id,
)
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
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
        self.flush_event = threading.Event()
        self.flushed_at = 0.0

    def _write_event(self, event: str, payload: dict[str, Any]) -> None:
        self.events.append((event, payload))
        self.flushed_at = time.monotonic()
        self.flush_event.set()


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
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
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
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
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

    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": "first", "done": False},
    )
    assert [payload["content"] for _event, payload in writer.events] == ["first"]

    for chunk in ("a" * 100, "b" * 100):
        writer.emit(
            "assistant_message_delta",
            {"message_id": "message-1", "chunk": chunk, "done": False},
        )
    assert len(writer.events) == 1

    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": "c" * 56, "done": False},
    )

    assert len(writer.events) == 2
    assert writer.events[1][0] == "token"
    assert writer.events[1][1]["content"] == "a" * 100 + "b" * 100 + "c" * 56


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
    assert [payload["content"] for _event, payload in writer.events] == ["first"]

    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": " second", "done": False},
    )
    clock[0] = 0.051
    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": " third", "done": False},
    )

    assert [payload["content"] for _event, payload in writer.events] == [
        "first",
        " second third",
    ]


def test_followup_short_assistant_delta_flushes_on_the_50ms_deadline_without_another_emit():
    writer = CapturingEventWriter(
        SimpleNamespace(
            agent=SimpleNamespace(event_flush_interval_ms=50, event_flush_max_chars=256)
        )
    )
    started_at = time.monotonic()
    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": "short", "done": False},
    )
    writer.flush_event.clear()
    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": " followup", "done": False},
    )

    assert writer.flush_event.wait(timeout=0.12)
    assert writer.flushed_at - started_at <= 0.08
    assert [payload["content"] for _event, payload in writer.events] == ["short", " followup"]
    writer.close()


def test_first_delta_is_immediate_when_a_message_id_is_reused_after_completion():
    writer = CapturingEventWriter(
        SimpleNamespace(
            agent=SimpleNamespace(event_flush_interval_ms=50, event_flush_max_chars=256)
        )
    )
    delta = {"message_id": "message-1", "chunk": "first", "done": False}

    writer.emit("assistant_message_delta", delta)
    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-1", "chunk": "", "done": True},
    )
    writer.emit("assistant_message_delta", delta)

    assert [payload["content"] for _event, payload in writer.events] == ["first", "", "first"]
    writer.close()


def test_execution_event_replay_validates_the_redis_cursor_only_once(monkeypatch):
    import contentai.services.conversation_service as conversation_module
    from contentai.models.enums import RunStatus
    from contentai.services.conversation_service import ConversationService

    execution = SimpleNamespace(
        first_event_at=None,
        streaming_degraded=False,
        streaming_degraded_reason="",
        status=RunStatus.running,
    )
    read_calls: list[bool] = []

    class FakeScope:
        def require_execution(self, **_kwargs):
            return SimpleNamespace(execution=execution)

    class FakeSession:
        def __init__(self, _bind):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _identifier):
            return execution

    class FakeStream:
        def __init__(self):
            self.batches = [
                [StreamEvent(1, "execution-1", "token", datetime.now(UTC), {"content": "a"})],
                [StreamEvent(2, "execution-1", "done", datetime.now(UTC), {})],
            ]

        def read(self, _execution_id, **kwargs):
            read_calls.append(kwargs["validate_cursor"])
            return self.batches.pop(0)

    stream = FakeStream()
    monkeypatch.setattr(conversation_module, "Session", FakeSession)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())

    events = list(
        service.replay_execution_events(
            object(), "execution-1", None, poll_interval_seconds=0.1
        )
    )

    assert [event_name for event_name, _payload in events] == ["token", "done"]
    assert read_calls == [True, False]


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
        assert [payload["content"] for _event, payload in writer.events] == ["tail"]
        writer.emit(terminal_event, {"status": terminal_event})

        assert writer.events[0][0] == "token"
        assert writer.events[0][1]["content"] == "tail"
        assert len(writer.events) == 2
