from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import contentai.agent.runtime.events as runtime_events
import contentai.services.event_stream as event_stream_module
import pytest
from contentai.agent.runtime.events import AgentEventWriter, PersistentAgentEventWriter
from contentai.db.session import get_engine
from contentai.models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatSession,
)
from contentai.models.enums import ExecutionAttemptStatus, RunStatus
from contentai.services.event_stream import (
    MAX_SAFE_EVENT_SEQUENCE,
    EventStreamPayloadInvalid,
    EventStreamPublishExpired,
    EventStreamPublishGap,
    EventStreamUnavailable,
    InvalidStreamCursor,
    RedisEventStream,
    StreamEvent,
    StreamReplayExpired,
    StreamReplayGap,
    close_cached_event_streams,
    execution_sequence_key,
    execution_stream_key,
    execution_terminal_sequence_key,
    redis_stream_id,
)
from contentai.services.execution_settlement import (
    current_database_time,
    finish_current_attempt,
    mark_streaming_degraded_first_wins,
    settle_execution_cancellation,
)
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID
from redis import Redis
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import inspect, text
from sqlmodel import Session, select


class FakePipeline:
    def __init__(self) -> None:
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []

    def xadd(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("xadd", args, kwargs))

    def expire(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("expire", args, kwargs))

    def execute(self) -> list[Any]:
        return [True] * len(self.commands)


class FakeBoundsPipeline:
    def __init__(self, redis: Any) -> None:
        self.redis = redis
        self.commands: list[tuple[str, tuple[Any, ...], dict[str, Any]]] = []
        self.execute_calls = 0

    def xrange(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("xrange", args, kwargs))

    def xrevrange(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("xrevrange", args, kwargs))

    def exists(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("exists", args, kwargs))

    def get(self, *args: Any, **kwargs: Any) -> None:
        self.commands.append(("get", args, kwargs))

    def execute(self) -> list[Any]:
        self.execute_calls += 1
        stream_exists = bool(self.redis.stream_exists)
        stream_key = str(self.commands[0][1][0])
        execution_id = stream_key.removesuffix(":events").rsplit(":", 1)[-1]
        return [
            (
                self.redis._row(self.redis.earliest, execution_id=execution_id)
                if stream_exists
                else []
            ),
            (
                self.redis._row(self.redis.latest, execution_id=execution_id)
                if stream_exists
                else []
            ),
            int(stream_exists),
            (
                str(self.redis.sequence_value)
                if self.redis.sequence_exists and self.redis.sequence_value is not None
                else None
            ),
            self.redis.terminal_raw,
        ]


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


class _SingleResult:
    def __init__(self, value: Any) -> None:
        self.value = value

    def one(self) -> Any:
        return self.value

    def one_or_none(self) -> Any:
        return self.value


def _fake_replay_execution(
    *,
    status: RunStatus = RunStatus.running,
    first_event_at: datetime | None = None,
    streaming_degraded: bool = False,
    streaming_degraded_reason: str = "",
    current_attempt_id: str | None = "attempt-execution-1",
    stream_committed_sequence: int = 0,
    terminal_stream_sequence: int | None = None,
    terminal_stream_attempt_id: str | None = None,
    terminal_stream_status: str | None = None,
) -> SimpleNamespace:
    if terminal_stream_sequence is not None:
        terminal_stream_attempt_id = terminal_stream_attempt_id or current_attempt_id
        terminal_stream_status = terminal_stream_status or status.value
    return SimpleNamespace(
        id="execution-1",
        first_event_at=first_event_at,
        streaming_degraded=streaming_degraded,
        streaming_degraded_reason=streaming_degraded_reason,
        status=status,
        current_attempt_id=current_attempt_id,
        stream_committed_sequence=stream_committed_sequence,
        terminal_stream_sequence=terminal_stream_sequence,
        terminal_stream_attempt_id=terminal_stream_attempt_id,
        terminal_stream_status=terminal_stream_status,
        touch_updated_at=lambda _timestamp: None,
    )


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


class BoundedFakeRedis(FakeRedis):
    def __init__(
        self,
        *,
        earliest: int | None,
        latest: int | None,
        stream_exists: bool = True,
        sequence_exists: bool = True,
        sequence_value: int | None = None,
        terminal_marker: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self.earliest = earliest
        self.latest = latest
        self.stream_exists = stream_exists
        self.sequence_exists = sequence_exists
        self.sequence_value = (
            latest if latest is not None else (1 if sequence_exists else None)
        ) if sequence_value is None else sequence_value
        self.terminal_marker = terminal_marker
        self.terminal_raw = (
            json.dumps(terminal_marker, separators=(",", ":"))
            if terminal_marker is not None
            else None
        )
        self.pipeline_transactions: list[bool] = []
        self.bounds_pipelines: list[FakeBoundsPipeline] = []

    def pipeline(self, *, transaction: bool) -> FakePipeline | FakeBoundsPipeline:
        self.pipeline_transactions.append(transaction)
        if not transaction:
            return super().pipeline(transaction=transaction)
        pipeline = FakeBoundsPipeline(self)
        self.bounds_pipelines.append(pipeline)
        return pipeline

    def _row(
        self,
        sequence: int | None,
        *,
        execution_id: str = "exe-1",
    ) -> list[tuple[str, dict[str, str]]]:
        if sequence is None:
            return []
        fields = {
            "sequence": str(sequence),
            "execution_id": execution_id,
            "type": "state",
        }
        if sequence == self.latest and self.terminal_marker is not None:
            fields["type"] = (
                "error"
                if self.terminal_marker["status"] == "failed"
                and self.terminal_marker["event_name"] == "run_error"
                else "state"
            )
            fields["data"] = json.dumps(
                {
                    "attempt_id": self.terminal_marker["attempt_id"],
                    "name": self.terminal_marker["event_name"],
                    "terminal_status": self.terminal_marker["status"],
                },
                separators=(",", ":"),
            )
        return [(f"{sequence}-0", fields)]

    def xrange(self, *_args: Any, **_kwargs: Any):
        return self._row(self.earliest)

    def xrevrange(self, *_args: Any, **_kwargs: Any):
        return self._row(self.latest)

    def exists(self, key: str) -> int:
        if key == execution_stream_key("exe-1"):
            return int(self.stream_exists)
        if key == execution_sequence_key("exe-1"):
            return int(self.sequence_exists)
        return 0


def _real_redis_stream() -> tuple[Redis, RedisEventStream]:
    client = Redis.from_url(
        "redis://127.0.0.1:6379/15",
        decode_responses=True,
        socket_connect_timeout=0.5,
        socket_timeout=1.0,
    )
    return client, RedisEventStream(client, ttl_seconds=60, max_length=100, block_ms=25)


def _redis_publication_state(client: Redis, execution_id: str) -> tuple[Any, ...]:
    stream_key = execution_stream_key(execution_id)
    return (
        int(client.exists(stream_key)),
        client.xrange(stream_key, min="-", max="+"),
        client.get(execution_sequence_key(execution_id)),
        client.get(execution_terminal_sequence_key(execution_id)),
    )


def _clear_redis_publication_state(client: Redis, execution_id: str) -> None:
    client.delete(
        execution_stream_key(execution_id),
        execution_sequence_key(execution_id),
        execution_terminal_sequence_key(execution_id),
    )


def _seed_redis_stream_entry(
    client: Redis,
    execution_id: str,
    *,
    stream_sequence: int,
    field_sequence: int | None = None,
    payload: dict[str, Any] | None = None,
    event_type: str = "state",
) -> None:
    client.xadd(
        execution_stream_key(execution_id),
        {
            "sequence": str(
                stream_sequence if field_sequence is None else field_sequence
            ),
            "execution_id": execution_id,
            "type": event_type,
            "timestamp": datetime.now(UTC).isoformat(),
            "data": json.dumps(payload or {}, separators=(",", ":")),
        },
        id=redis_stream_id(stream_sequence),
    )


def test_publish_client_uses_cached_timeout_independent_from_blocking_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    created: list[tuple[object, dict[str, Any]]] = []

    def from_url(url: str, **kwargs: Any) -> object:
        client = object()
        created.append((client, {"url": url, **kwargs}))
        return client

    monkeypatch.setattr(
        event_stream_module.Redis,
        "from_url",
        staticmethod(from_url),
    )
    settings = SimpleNamespace(
        redis=SimpleNamespace(
            url="redis://publisher-timeout.test/0",
            event_ttl_seconds=60,
            event_max_length=100,
            event_block_ms=5_000,
        )
    )
    publish_key = (settings.redis.url, 60, 100)
    read_key = (settings.redis.url, 60, 100, 5_000)
    event_stream_module._PUBLISH_STREAM_INSTANCES.pop(publish_key, None)
    event_stream_module._STREAM_INSTANCES.pop(read_key, None)
    try:
        publisher = RedisEventStream.publisher_from_settings(settings)
        assert RedisEventStream.publisher_from_settings(settings) is publisher
        reader = RedisEventStream.from_settings(settings)

        assert publisher is not reader
        assert len(created) == 2
        assert 0 < created[0][1]["socket_timeout"] <= 1.0
        assert created[1][1]["socket_timeout"] > created[0][1]["socket_timeout"]
    finally:
        close_cached_event_streams()


def test_cached_event_stream_factory_is_singleton_under_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_cached_event_streams()
    created: list[object] = []
    created_lock = threading.Lock()

    def from_url(_url: str, **_kwargs: Any) -> object:
        client = object()
        with created_lock:
            created.append(client)
        return client

    monkeypatch.setattr(event_stream_module.Redis, "from_url", staticmethod(from_url))
    settings = SimpleNamespace(
        redis=SimpleNamespace(
            url="redis://concurrent-cache.test/0",
            event_ttl_seconds=60,
            event_max_length=100,
            event_block_ms=5_000,
        )
    )
    start = threading.Barrier(9)

    def load_publisher() -> RedisEventStream:
        start.wait(timeout=3)
        return RedisEventStream.publisher_from_settings(settings)

    try:
        with ThreadPoolExecutor(max_workers=8) as executor:
            futures = [executor.submit(load_publisher) for _ in range(8)]
            start.wait(timeout=3)
            instances = [future.result(timeout=3) for future in futures]
        assert len(created) == 1
        assert all(instance is instances[0] for instance in instances)
    finally:
        close_cached_event_streams()


def test_cached_event_stream_close_deduplicates_clients_and_is_idempotent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_cached_event_streams()

    class SharedClient:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    client = SharedClient()
    monkeypatch.setattr(
        event_stream_module.Redis,
        "from_url",
        staticmethod(lambda *_args, **_kwargs: client),
    )
    settings = SimpleNamespace(
        redis=SimpleNamespace(
            url="redis://shared-cache.test/0",
            event_ttl_seconds=60,
            event_max_length=100,
            event_block_ms=5_000,
        )
    )
    RedisEventStream.from_settings(settings)
    RedisEventStream.publisher_from_settings(settings)

    close_cached_event_streams()
    close_cached_event_streams()

    assert client.close_calls == 1


def test_cached_event_stream_close_continues_after_one_client_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    close_cached_event_streams()

    class Client:
        def __init__(self, *, fail: bool) -> None:
            self.fail = fail
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1
            if self.fail:
                raise RuntimeError("close failed")

    clients = iter((Client(fail=True), Client(fail=False)))
    created: list[Client] = []

    def from_url(*_args: Any, **_kwargs: Any) -> Client:
        client = next(clients)
        created.append(client)
        return client

    monkeypatch.setattr(event_stream_module.Redis, "from_url", staticmethod(from_url))
    settings = SimpleNamespace(
        redis=SimpleNamespace(
            url="redis://failing-close.test/0",
            event_ttl_seconds=60,
            event_max_length=100,
            event_block_ms=5_000,
        )
    )
    RedisEventStream.from_settings(settings)
    RedisEventStream.publisher_from_settings(settings)

    close_cached_event_streams()

    assert [client.close_calls for client in created] == [1, 1]
    assert "Failed to close cached Redis event stream client." in caplog.text


def test_cached_event_stream_rebuilds_after_shutdown_or_configuration_change(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    close_cached_event_streams()
    created: list[object] = []

    def from_url(*_args: Any, **_kwargs: Any) -> object:
        client = SimpleNamespace(close=lambda: None)
        created.append(client)
        return client

    monkeypatch.setattr(event_stream_module.Redis, "from_url", staticmethod(from_url))

    def settings(url: str) -> SimpleNamespace:
        return SimpleNamespace(
            redis=SimpleNamespace(
                url=url,
                event_ttl_seconds=60,
                event_max_length=100,
                event_block_ms=5_000,
            )
        )

    first = RedisEventStream.publisher_from_settings(settings("redis://cache-a.test/0"))
    close_cached_event_streams()
    second = RedisEventStream.publisher_from_settings(settings("redis://cache-a.test/0"))
    third = RedisEventStream.publisher_from_settings(settings("redis://cache-b.test/0"))
    close_cached_event_streams()

    assert first is not second
    assert second is not third
    assert len(created) == 3


def test_repeated_celery_shutdown_signals_close_worker_cache_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from contentai.services.celery_app import close_worker_event_streams

    close_cached_event_streams()

    class Client:
        def __init__(self) -> None:
            self.close_calls = 0

        def close(self) -> None:
            self.close_calls += 1

    client = Client()
    monkeypatch.setattr(
        event_stream_module.Redis,
        "from_url",
        staticmethod(lambda *_args, **_kwargs: client),
    )
    settings = SimpleNamespace(
        redis=SimpleNamespace(
            url="redis://worker-shutdown.test/0",
            event_ttl_seconds=60,
            event_max_length=100,
            event_block_ms=5_000,
        )
    )
    RedisEventStream.publisher_from_settings(settings)

    close_worker_event_streams()
    close_worker_event_streams()

    assert client.close_calls == 1


@pytest.mark.parametrize(
    ("redis_state", "error_type"),
    [
        ("double-empty-with-history", EventStreamPublishExpired),
        ("empty-stream-key", EventStreamPublishExpired),
        ("counter-only", EventStreamPublishExpired),
        ("stream-only", EventStreamPublishExpired),
        ("bad-counter", EventStreamPublishGap),
        ("counter-db-mismatch", EventStreamPublishGap),
        ("id-counter-mismatch", EventStreamPublishGap),
        ("field-counter-mismatch", EventStreamPublishGap),
        ("terminal-only", EventStreamPublishExpired),
        ("terminal-without-counter", EventStreamPublishExpired),
        ("terminal-empty-stream", EventStreamPublishExpired),
        ("terminal-latest-mismatch", EventStreamPublishGap),
    ],
)
def test_publish_lua_rejects_inconsistent_history_without_mutation(
    redis_state: str,
    error_type: type[Exception],
) -> None:
    execution_id = f"lua-reject-{redis_state}"
    client, stream = _real_redis_stream()
    stream_key = execution_stream_key(execution_id)
    sequence_key = execution_sequence_key(execution_id)
    terminal_key = execution_terminal_sequence_key(execution_id)
    expected_sequence = 1
    allow_initialize = True
    terminal_marker = json.dumps(
        {
            "sequence": "1",
            "attempt_id": "attempt-old",
            "event_name": "run_interrupt",
            "status": "waiting_input",
        },
        separators=(",", ":"),
    )
    _clear_redis_publication_state(client, execution_id)
    try:
        if redis_state == "double-empty-with-history":
            expected_sequence = 0
            allow_initialize = False
        elif redis_state == "empty-stream-key":
            _seed_redis_stream_entry(client, execution_id, stream_sequence=1)
            client.xdel(stream_key, redis_stream_id(1))
            assert client.exists(stream_key) == 1
            expected_sequence = 0
        elif redis_state == "counter-only":
            client.set(sequence_key, "1", ex=60)
        elif redis_state == "stream-only":
            _seed_redis_stream_entry(client, execution_id, stream_sequence=1)
        elif redis_state == "bad-counter":
            _seed_redis_stream_entry(client, execution_id, stream_sequence=1)
            client.set(sequence_key, "not-an-integer", ex=60)
        elif redis_state == "counter-db-mismatch":
            _seed_redis_stream_entry(client, execution_id, stream_sequence=1)
            client.set(sequence_key, "2", ex=60)
        elif redis_state == "id-counter-mismatch":
            _seed_redis_stream_entry(
                client,
                execution_id,
                stream_sequence=2,
                field_sequence=1,
            )
            client.set(sequence_key, "1", ex=60)
        elif redis_state == "field-counter-mismatch":
            _seed_redis_stream_entry(
                client,
                execution_id,
                stream_sequence=1,
                field_sequence=2,
            )
            client.set(sequence_key, "1", ex=60)
        elif redis_state == "terminal-only":
            client.set(terminal_key, terminal_marker, ex=60)
        elif redis_state == "terminal-without-counter":
            _seed_redis_stream_entry(client, execution_id, stream_sequence=1)
            client.set(terminal_key, terminal_marker, ex=60)
        elif redis_state == "terminal-empty-stream":
            _seed_redis_stream_entry(client, execution_id, stream_sequence=1)
            client.xdel(stream_key, redis_stream_id(1))
            assert client.exists(stream_key) == 1
            client.set(sequence_key, "1", ex=60)
            client.set(terminal_key, terminal_marker, ex=60)
        elif redis_state == "terminal-latest-mismatch":
            _seed_redis_stream_entry(client, execution_id, stream_sequence=2)
            client.set(sequence_key, "2", ex=60)
            client.set(terminal_key, terminal_marker, ex=60)
            expected_sequence = 2
        else:  # pragma: no cover - exhaustive parameter guard
            raise AssertionError(redis_state)

        before = _redis_publication_state(client, execution_id)
        with pytest.raises(error_type):
            stream.publish_event(
                execution_id=execution_id,
                event_type="state",
                payload={"name": "run_resume", "attempt_id": "attempt-new"},
                timestamp=datetime.now(UTC),
                expected_sequence=expected_sequence,
                allow_initialize=allow_initialize,
            )
        assert _redis_publication_state(client, execution_id) == before
    finally:
        _clear_redis_publication_state(client, execution_id)
        client.close()


@pytest.mark.parametrize(
    "redis_state",
    [
        "double-empty-init",
        "consistent-history",
        "close-stream",
        "already-closed",
        "reopen-waiting",
    ],
)
def test_publish_lua_advances_or_closes_only_from_consistent_history(
    redis_state: str,
) -> None:
    execution_id = f"lua-success-{redis_state}"
    client, stream = _real_redis_stream()
    sequence_key = execution_sequence_key(execution_id)
    terminal_key = execution_terminal_sequence_key(execution_id)
    _clear_redis_publication_state(client, execution_id)
    try:
        expected_sequence = 0
        close_stream = False
        allow_reopen = False
        terminal_attempt_id: str | None = None
        terminal_event_name: str | None = None
        terminal_status: str | None = None
        reopen_attempt_id: str | None = None
        reopen_status: str | None = None
        payload: dict[str, Any] = {"name": "run_start", "attempt_id": "attempt-new"}

        if redis_state != "double-empty-init":
            prior_payload = {
                "name": "run_interrupt",
                "attempt_id": "attempt-old",
                "terminal_status": "waiting_input",
            }
            _seed_redis_stream_entry(
                client,
                execution_id,
                stream_sequence=1,
                payload=prior_payload,
            )
            client.set(sequence_key, "1", ex=60)
            expected_sequence = 1
        if redis_state in {"already-closed", "reopen-waiting"}:
            client.set(
                terminal_key,
                json.dumps(
                    {
                        "sequence": "1",
                        "attempt_id": "attempt-old",
                        "event_name": "run_interrupt",
                        "status": "waiting_input",
                    },
                    separators=(",", ":"),
                ),
                ex=60,
            )
        if redis_state == "close-stream":
            close_stream = True
            terminal_attempt_id = "attempt-new"
            terminal_event_name = "run_finish"
            terminal_status = "completed"
            payload = {
                "name": "run_finish",
                "attempt_id": "attempt-new",
                "terminal_status": "completed",
            }
        elif redis_state == "already-closed":
            close_stream = True
            terminal_attempt_id = "attempt-old"
            terminal_event_name = "run_interrupt"
            terminal_status = "waiting_input"
            payload = {
                "name": "run_interrupt",
                "attempt_id": "attempt-old",
                "terminal_status": "waiting_input",
            }
        elif redis_state == "reopen-waiting":
            allow_reopen = True
            reopen_attempt_id = "attempt-old"
            reopen_status = "waiting_input"
            terminal_attempt_id = "attempt-new"
            payload = {"name": "run_resume", "attempt_id": "attempt-new"}

        before_length = client.xlen(execution_stream_key(execution_id))
        sequence = stream.publish_event(
            execution_id=execution_id,
            event_type="state",
            payload=payload,
            timestamp=datetime.now(UTC),
            expected_sequence=expected_sequence,
            allow_initialize=True,
            close_stream=close_stream,
            terminal_attempt_id=terminal_attempt_id,
            terminal_event_name=terminal_event_name,
            terminal_status=terminal_status,
            allow_reopen=allow_reopen,
            reopen_attempt_id=reopen_attempt_id,
            reopen_status=reopen_status,
        )

        if redis_state == "already-closed":
            assert sequence == 1
            assert client.xlen(execution_stream_key(execution_id)) == before_length
            assert client.get(terminal_key) is not None
        else:
            assert sequence == expected_sequence + 1
            assert client.xlen(execution_stream_key(execution_id)) == before_length + 1
            assert client.get(sequence_key) == str(sequence)
        if redis_state == "close-stream":
            marker = json.loads(client.get(terminal_key) or "{}")
            assert marker == {
                "sequence": "2",
                "attempt_id": "attempt-new",
                "event_name": "run_finish",
                "status": "completed",
            }
        elif redis_state == "reopen-waiting":
            assert client.get(terminal_key) is None
    finally:
        _clear_redis_publication_state(client, execution_id)
        client.close()


@pytest.mark.parametrize("operation", ["already-closed", "reopen-waiting"])
@pytest.mark.parametrize(
    "corruption",
    [
        "marker-sequence-number",
        "marker-attempt-number",
        "marker-event-number",
        "marker-status-number",
        "latest-attempt-number",
        "latest-name-number",
        "latest-status-number",
        "latest-identity-mismatch",
        "latest-malformed-json",
    ],
)
def test_terminal_replay_identity_must_be_strict_before_close_or_reopen(
    operation: str,
    corruption: str,
) -> None:
    execution_id = f"lua-terminal-identity-{operation}-{corruption}"
    client, stream = _real_redis_stream()
    marker: dict[str, Any] = {
        "sequence": "1",
        "attempt_id": "attempt-old",
        "event_name": "run_interrupt",
        "status": "waiting_input",
    }
    latest_payload: dict[str, Any] = {
        "attempt_id": "attempt-old",
        "name": "run_interrupt",
        "terminal_status": "waiting_input",
    }
    malformed_latest = corruption == "latest-malformed-json"
    if corruption == "marker-sequence-number":
        marker["sequence"] = 1
    elif corruption == "marker-attempt-number":
        marker["attempt_id"] = 7
    elif corruption == "marker-event-number":
        marker["event_name"] = 7
    elif corruption == "marker-status-number":
        marker["status"] = 7
    elif corruption == "latest-attempt-number":
        latest_payload["attempt_id"] = 7
    elif corruption == "latest-name-number":
        latest_payload["name"] = 7
    elif corruption == "latest-status-number":
        latest_payload["terminal_status"] = 7
    elif corruption == "latest-identity-mismatch":
        latest_payload["attempt_id"] = "attempt-tampered"

    _clear_redis_publication_state(client, execution_id)
    try:
        client.xadd(
            execution_stream_key(execution_id),
            {
                "sequence": "1",
                "execution_id": execution_id,
                "type": "state",
                "timestamp": datetime.now(UTC).isoformat(),
                "data": (
                    "{malformed"
                    if malformed_latest
                    else json.dumps(latest_payload, separators=(",", ":"))
                ),
            },
            id="1-0",
        )
        client.set(execution_sequence_key(execution_id), "1", ex=60)
        client.set(
            execution_terminal_sequence_key(execution_id),
            json.dumps(marker, separators=(",", ":")),
            ex=60,
        )
        before = _redis_publication_state(client, execution_id)
        kwargs: dict[str, Any]
        if operation == "already-closed":
            kwargs = {
                "payload": {
                    "attempt_id": "attempt-old",
                    "name": "run_interrupt",
                    "terminal_status": "waiting_input",
                },
                "close_stream": True,
                "terminal_attempt_id": "attempt-old",
                "terminal_event_name": "run_interrupt",
                "terminal_status": "waiting_input",
            }
        else:
            kwargs = {
                "payload": {"attempt_id": "attempt-new", "name": "run_resume"},
                "allow_reopen": True,
                "terminal_attempt_id": "attempt-new",
                "reopen_attempt_id": "attempt-old",
                "reopen_status": "waiting_input",
            }

        with pytest.raises(EventStreamPublishGap):
            stream.publish_event(
                execution_id=execution_id,
                event_type="state",
                timestamp=datetime.now(UTC),
                expected_sequence=1,
                allow_initialize=False,
                **kwargs,
            )

        assert _redis_publication_state(client, execution_id) == before
    finally:
        _clear_redis_publication_state(client, execution_id)
        client.close()


@pytest.mark.parametrize("operation", ["already-closed", "reopen-waiting"])
@pytest.mark.parametrize(
    "tamper",
    [
        "execution-id-wrong",
        "execution-id-missing",
        "execution-id-duplicate",
        "execution-id-number",
        "type-wrong",
        "type-missing",
        "type-duplicate",
        "type-number",
        "status-event-mismatch",
    ],
)
def test_terminal_latest_required_fields_gate_close_and_reopen_before_mutation(
    operation: str,
    tamper: str,
) -> None:
    execution_id = f"lua-terminal-fields-{operation}-{tamper}"
    client, stream = _real_redis_stream()
    marker: dict[str, Any] = {
        "sequence": "1",
        "attempt_id": "attempt-old",
        "event_name": "run_interrupt",
        "status": "waiting_input",
    }
    payload: dict[str, Any] = {
        "attempt_id": "attempt-old",
        "name": "run_interrupt",
        "terminal_status": "waiting_input",
    }
    fields: list[tuple[str, Any]] = [
        ("sequence", "1"),
        ("execution_id", execution_id),
        ("type", "state"),
        ("timestamp", datetime.now(UTC).isoformat()),
        ("data", json.dumps(payload, separators=(",", ":"))),
    ]
    if tamper == "execution-id-wrong":
        fields[1] = ("execution_id", "execution-other")
    elif tamper == "execution-id-missing":
        fields.pop(1)
    elif tamper == "execution-id-duplicate":
        fields.append(("execution_id", execution_id))
    elif tamper == "execution-id-number":
        fields[1] = ("execution_id", 7)
    elif tamper == "type-wrong":
        fields[2] = ("type", "error")
    elif tamper == "type-missing":
        fields.pop(2)
    elif tamper == "type-duplicate":
        fields.append(("type", "state"))
    elif tamper == "type-number":
        fields[2] = ("type", 7)
    elif tamper == "status-event-mismatch":
        marker["event_name"] = "run_finish"
        payload["name"] = "run_finish"
        fields[-1] = ("data", json.dumps(payload, separators=(",", ":")))

    _clear_redis_publication_state(client, execution_id)
    try:
        command: list[Any] = ["XADD", execution_stream_key(execution_id), "1-0"]
        for field_name, value in fields:
            command.extend((field_name, value))
        client.execute_command(*command)
        client.set(execution_sequence_key(execution_id), "1", ex=60)
        client.set(
            execution_terminal_sequence_key(execution_id),
            json.dumps(marker, separators=(",", ":")),
            ex=60,
        )
        before = _redis_publication_state(client, execution_id)
        if tamper not in {"execution-id-duplicate", "type-duplicate"}:
            with pytest.raises(StreamReplayGap):
                stream.bounds(execution_id)

        kwargs: dict[str, Any]
        if operation == "already-closed":
            kwargs = {
                "payload": {
                    "attempt_id": "attempt-old",
                    "name": "run_interrupt",
                    "terminal_status": "waiting_input",
                },
                "close_stream": True,
                "terminal_attempt_id": "attempt-old",
                "terminal_event_name": "run_interrupt",
                "terminal_status": "waiting_input",
            }
        else:
            kwargs = {
                "payload": {"attempt_id": "attempt-new", "name": "run_resume"},
                "allow_reopen": True,
                "terminal_attempt_id": "attempt-new",
                "reopen_attempt_id": "attempt-old",
                "reopen_status": "waiting_input",
            }
        with pytest.raises(EventStreamPublishGap):
            stream.publish_event(
                execution_id=execution_id,
                event_type="state",
                timestamp=datetime.now(UTC),
                expected_sequence=1,
                allow_initialize=False,
                **kwargs,
            )
        assert _redis_publication_state(client, execution_id) == before
    finally:
        _clear_redis_publication_state(client, execution_id)
        client.close()


@pytest.mark.parametrize(
    ("status", "event_name", "event_type"),
    [
        ("waiting_input", "run_interrupt", "state"),
        ("completed", "run_finish", "state"),
        ("failed", "run_error", "error"),
        ("cancelled", "run_cancel", "state"),
    ],
)
def test_four_official_terminal_types_are_canonical_for_bounds_and_idempotent_close(
    status: str,
    event_name: str,
    event_type: str,
) -> None:
    execution_id = f"lua-terminal-canonical-{status}"
    client, stream = _real_redis_stream()
    _clear_redis_publication_state(client, execution_id)
    try:
        payload = {
            "attempt_id": "attempt-terminal",
            "name": event_name,
            "terminal_status": status,
        }
        _seed_redis_stream_entry(
            client,
            execution_id,
            stream_sequence=1,
            payload=payload,
            event_type=event_type,
        )
        client.set(execution_sequence_key(execution_id), "1", ex=60)
        client.set(
            execution_terminal_sequence_key(execution_id),
            json.dumps(
                {
                    "sequence": "1",
                    "attempt_id": "attempt-terminal",
                    "event_name": event_name,
                    "status": status,
                },
                separators=(",", ":"),
            ),
            ex=60,
        )
        before = _redis_publication_state(client, execution_id)

        bounds = stream.bounds(execution_id)
        assert bounds.terminal_sequence == 1
        assert bounds.terminal_event_name == event_name
        assert bounds.terminal_status == status
        assert stream.publish_event(
            execution_id=execution_id,
            event_type=event_type,
            payload=payload,
            timestamp=datetime.now(UTC),
            expected_sequence=1,
            allow_initialize=False,
            close_stream=True,
            terminal_attempt_id="attempt-terminal",
            terminal_event_name=event_name,
            terminal_status=status,
        ) == 1
        assert _redis_publication_state(client, execution_id) == before
    finally:
        _clear_redis_publication_state(client, execution_id)
        client.close()


def test_publish_lua_rejects_max_safe_sequence_without_mutation() -> None:
    execution_id = "lua-max-safe-sequence"
    client, stream = _real_redis_stream()
    _clear_redis_publication_state(client, execution_id)
    try:
        _seed_redis_stream_entry(
            client,
            execution_id,
            stream_sequence=MAX_SAFE_EVENT_SEQUENCE,
        )
        client.set(
            execution_sequence_key(execution_id),
            str(MAX_SAFE_EVENT_SEQUENCE),
            ex=60,
        )
        before = _redis_publication_state(client, execution_id)
        with pytest.raises(EventStreamPublishGap):
            stream.publish_event(
                execution_id=execution_id,
                event_type="state",
                payload={"name": "run_start"},
                timestamp=datetime.now(UTC),
                expected_sequence=MAX_SAFE_EVENT_SEQUENCE,
            )
        assert _redis_publication_state(client, execution_id) == before
    finally:
        _clear_redis_publication_state(client, execution_id)
        client.close()


def test_reopen_script_never_deletes_terminal_marker_before_xadd() -> None:
    script = event_stream_module._PUBLISH_EVENT_LUA
    assert script.index("redis.call('XADD'") < script.index(
        "redis.call('DEL', KEYS[3])"
    )


def test_reopen_xadd_failure_keeps_prior_terminal_marker_and_history(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = "lua-reopen-xadd-failure"
    client, stream = _real_redis_stream()
    _clear_redis_publication_state(client, execution_id)
    try:
        _seed_redis_stream_entry(
            client,
            execution_id,
            stream_sequence=1,
            payload={
                "name": "run_interrupt",
                "attempt_id": "attempt-old",
                "terminal_status": "waiting_input",
            },
        )
        client.set(execution_sequence_key(execution_id), "1", ex=60)
        client.set(
            execution_terminal_sequence_key(execution_id),
            json.dumps(
                {
                    "sequence": "1",
                    "attempt_id": "attempt-old",
                    "event_name": "run_interrupt",
                    "status": "waiting_input",
                },
                separators=(",", ":"),
            ),
            ex=60,
        )
        broken_script = event_stream_module._PUBLISH_EVENT_LUA.replace(
            "local stream_id = next_sequence_raw .. '-0'",
            "local stream_id = '0-0'",
        )
        assert broken_script != event_stream_module._PUBLISH_EVENT_LUA
        monkeypatch.setattr(event_stream_module, "_PUBLISH_EVENT_LUA", broken_script)
        before = _redis_publication_state(client, execution_id)

        with pytest.raises(EventStreamUnavailable):
            stream.publish_event(
                execution_id=execution_id,
                event_type="state",
                payload={"name": "run_resume", "attempt_id": "attempt-new"},
                timestamp=datetime.now(UTC),
                expected_sequence=1,
                allow_initialize=False,
                allow_reopen=True,
                terminal_attempt_id="attempt-new",
                reopen_attempt_id="attempt-old",
                reopen_status="waiting_input",
            )

        assert _redis_publication_state(client, execution_id) == before
    finally:
        _clear_redis_publication_state(client, execution_id)
        client.close()


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


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_publish_event_rejects_non_finite_payload_before_redis(non_finite) -> None:
    class NoWriteRedis:
        def __init__(self) -> None:
            self.eval_calls = 0

        def eval(self, *_args: Any, **_kwargs: Any) -> int:
            self.eval_calls += 1
            return 1

    redis = NoWriteRedis()
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(EventStreamPayloadInvalid):
        stream.publish_event(
            execution_id="exe-1",
            event_type="token",
            payload={"value": non_finite},
            timestamp=datetime(2026, 8, 4, 11, 0, tzinfo=UTC),
        )

    assert redis.eval_calls == 0


def test_publish_event_keeps_payload_and_redis_failures_distinct() -> None:
    class FailingRedis:
        def __init__(self) -> None:
            self.eval_calls = 0

        def eval(self, *_args: Any, **_kwargs: Any) -> Any:
            self.eval_calls += 1
            raise event_stream_module.RedisError("connection failed")

    redis = FailingRedis()
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)
    with pytest.raises(EventStreamPayloadInvalid):
        stream.publish_event(
            execution_id="payload-error",
            event_type="state",
            payload={"value": object()},
            timestamp=datetime.now(UTC),
        )
    assert redis.eval_calls == 0

    with pytest.raises(EventStreamUnavailable) as caught:
        stream.publish_event(
            execution_id="redis-error",
            event_type="state",
            payload={"value": "valid"},
            timestamp=datetime.now(UTC),
        )
    assert type(caught.value) is EventStreamUnavailable
    assert redis.eval_calls == 1


@pytest.mark.parametrize("non_finite", [float("nan"), float("inf"), float("-inf")])
def test_explicit_publish_rejects_non_finite_payload_before_pipeline(non_finite) -> None:
    redis = FakeRedis()
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(EventStreamUnavailable):
        stream.publish(
            [
                StreamEvent(
                    sequence=1,
                    execution_id="exe-1",
                    event_type="token",
                    timestamp=datetime(2026, 8, 4, 11, 0, tzinfo=UTC),
                    payload={"value": non_finite},
                )
            ]
        )

    assert redis.last_pipeline is None


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

    assert stream.bounds("exe-1").bounds_known is False
    events = stream.read("exe-1", after_sequence=7)

    assert redis.read_args == ({execution_stream_key("exe-1"): redis_stream_id(7)},)
    assert redis.read_kwargs == {"block": 1234}
    assert len(events) == 1
    assert events[0].sequence == 8
    assert events[0].event_type == "done"
    assert events[0].payload == {"status": "completed"}


def test_replay_accepts_payload_at_deterministic_nesting_limit() -> None:
    redis = FakeRedis()
    redis.read_response = [
        (
            execution_stream_key("exe-1"),
            [
                (
                    "1-0",
                    {
                        "sequence": "1",
                        "execution_id": "exe-1",
                        "type": "token",
                        "data": '{"value":' + "[" * 128 + "0" + "]" * 128 + "}",
                    },
                )
            ],
        )
    ]
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    events = stream.read("exe-1", after_sequence=0)

    assert len(events) == 1
    assert events[0].sequence == 1


@pytest.mark.parametrize(
    "legacy_payload",
    [
        pytest.param('{"value":NaN}', id="nan"),
        pytest.param('{"value":Infinity}', id="positive-infinity"),
        pytest.param('{"value":+Infinity}', id="explicit-positive-infinity"),
        pytest.param('{"value":-Infinity}', id="negative-infinity"),
        pytest.param('{"value":1e999}', id="top-level-exponent-overflow"),
        pytest.param(
            '{"nested":{"items":[1,{"value":1e999}]}}',
            id="nested-exponent-overflow",
        ),
        pytest.param(
            '{"value":' + "[" * 129 + "0" + "]" * 129 + "}",
            id="over-nesting-limit",
        ),
        pytest.param(
            '{"value":' + "[" * 2_000 + "0" + "]" * 2_000 + "}",
            id="overdeep-json",
        ),
        pytest.param('{"value":', id="malformed"),
        pytest.param('["legacy-array"]', id="array"),
    ],
)
def test_replay_rejects_invalid_legacy_payload_without_yielding(legacy_payload) -> None:
    redis = FakeRedis()
    redis.read_response = [
        (
            execution_stream_key("exe-1"),
            [
                (
                    "1-0",
                    {
                        "sequence": "1",
                        "execution_id": "exe-1",
                        "type": "token",
                        "data": '{"content":"valid-prefix"}',
                    },
                ),
                (
                    "2-0",
                    {
                        "sequence": "2",
                        "execution_id": "exe-1",
                        "type": "token",
                        "data": legacy_payload,
                    },
                ),
            ],
        )
    ]
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)
    replay = stream.iter_events("exe-1", after_sequence=0)

    with pytest.raises(EventStreamUnavailable) as raised:
        next(replay)

    assert str(raised.value) == "Invalid Redis execution event payload"
    assert legacy_payload not in str(raised.value)
    assert redis.read_args == ({execution_stream_key("exe-1"): redis_stream_id(0)},)


def test_replay_rejects_missing_data_without_yielding_valid_prefix() -> None:
    redis = FakeRedis()
    redis.read_response = [
        (
            execution_stream_key("exe-1"),
            [
                (
                    "1-0",
                    {
                        "sequence": "1",
                        "execution_id": "exe-1",
                        "type": "token",
                        "data": '{"content":"valid-prefix"}',
                    },
                ),
                (
                    "2-0",
                    {
                        "sequence": "2",
                        "execution_id": "exe-1",
                        "type": "token",
                    },
                ),
            ],
        )
    ]
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)
    replay = stream.iter_events("exe-1", after_sequence=0)

    with pytest.raises(EventStreamUnavailable) as raised:
        next(replay)

    assert str(raised.value) == "Invalid Redis execution event payload"
    assert redis.read_args == ({execution_stream_key("exe-1"): redis_stream_id(0)},)


def test_cursor_validation_rejects_trimmed_initial_history() -> None:
    redis = BoundedFakeRedis(earliest=5, latest=8)
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(StreamReplayGap, match="stream begins at 5"):
        stream.validate_cursor("exe-1", after_sequence=2)


def test_cursor_validation_prioritizes_committed_gap_over_future_cursor() -> None:
    redis = BoundedFakeRedis(earliest=1, latest=2)
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(
        StreamReplayGap,
        match="Redis and database committed watermarks disagree",
    ):
        stream.validate_cursor(
            "exe-1",
            after_sequence=3,
            committed_sequence=1,
        )


def test_cursor_validation_keeps_future_cursor_invalid_when_watermarks_agree() -> None:
    redis = BoundedFakeRedis(earliest=1, latest=2)
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(InvalidStreamCursor, match="after latest sequence 2"):
        stream.validate_cursor(
            "exe-1",
            after_sequence=3,
            committed_sequence=2,
        )


def test_bounds_uses_one_transaction_snapshot_without_drifting_direct_reads() -> None:
    class DriftingRedis(BoundedFakeRedis):
        def __init__(self) -> None:
            super().__init__(earliest=1, latest=3)
            self.direct_calls = 0

        def xrange(self, *_args: Any, **_kwargs: Any):
            self.direct_calls += 1
            self.stream_exists = False
            return self._row(1)

        def xrevrange(self, *_args: Any, **_kwargs: Any):
            self.direct_calls += 1
            return []

        def exists(self, _key: str) -> int:
            self.direct_calls += 1
            return 0

    redis = DriftingRedis()
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    bounds = stream.bounds("exe-1")

    assert bounds.earliest_sequence == 1
    assert bounds.latest_sequence == 3
    assert bounds.stream_exists is True
    assert bounds.sequence_exists is True
    assert redis.direct_calls == 0
    assert redis.pipeline_transactions == [True]
    assert len(redis.bounds_pipelines) == 1
    pipeline = redis.bounds_pipelines[0]
    assert pipeline.execute_calls == 1
    assert pipeline.commands == [
        ("xrange", (execution_stream_key("exe-1"),), {"min": "-", "max": "+", "count": 1}),
        ("xrevrange", (execution_stream_key("exe-1"),), {"max": "+", "min": "-", "count": 1}),
        ("exists", (execution_stream_key("exe-1"),), {}),
        ("get", (execution_sequence_key("exe-1"),), {}),
        ("get", (execution_terminal_sequence_key("exe-1"),), {}),
    ]


@pytest.mark.parametrize(
    "redis_state",
    [
        "counter-only-invalid",
        "stream-only-invalid-entry",
        "empty-stream-with-counter",
        "terminal-only-malformed",
    ],
)
def test_bounds_prioritizes_one_sided_expiration_over_value_gaps(
    redis_state: str,
) -> None:
    if redis_state == "counter-only-invalid":
        redis = BoundedFakeRedis(
            earliest=None,
            latest=None,
            stream_exists=False,
            sequence_exists=True,
            sequence_value="not-an-integer",  # type: ignore[arg-type]
        )
    elif redis_state == "stream-only-invalid-entry":
        redis = BoundedFakeRedis(
            earliest=1,
            latest=2,
            stream_exists=True,
            sequence_exists=False,
        )
        redis.latest = MAX_SAFE_EVENT_SEQUENCE + 1
    elif redis_state == "empty-stream-with-counter":
        redis = BoundedFakeRedis(
            earliest=None,
            latest=None,
            stream_exists=True,
            sequence_exists=True,
            sequence_value="not-an-integer",  # type: ignore[arg-type]
        )
    else:
        redis = BoundedFakeRedis(
            earliest=None,
            latest=None,
            stream_exists=False,
            sequence_exists=False,
        )
        redis.terminal_raw = "{malformed"

    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(StreamReplayExpired):
        stream.bounds("exe-1")


def test_bounds_reports_value_gap_only_after_stream_and_counter_are_complete() -> None:
    redis = BoundedFakeRedis(
        earliest=1,
        latest=2,
        stream_exists=True,
        sequence_exists=True,
        sequence_value="not-an-integer",  # type: ignore[arg-type]
    )
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(StreamReplayGap, match="sequence counter"):
        stream.bounds("exe-1")


def test_cursor_validation_rejects_nonzero_cursor_before_first_event() -> None:
    redis = BoundedFakeRedis(
        earliest=None,
        latest=None,
        stream_exists=False,
        sequence_exists=False,
    )
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    assert stream.bounds("exe-1").bounds_known is True
    with pytest.raises(InvalidStreamCursor, match="after latest sequence 0"):
        stream.validate_cursor("exe-1", after_sequence=1)


def test_cursor_validation_rejects_orphaned_sequence_key_without_database_marker() -> None:
    redis = BoundedFakeRedis(
        earliest=None,
        latest=None,
        stream_exists=False,
        sequence_exists=True,
    )
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(StreamReplayExpired):
        stream.validate_cursor("exe-1", after_sequence=0)


@pytest.mark.parametrize("cursor", [0, 2])
def test_cursor_validation_rejects_redis_terminal_without_database_terminal(
    cursor: int,
) -> None:
    redis = BoundedFakeRedis(
        earliest=1,
        latest=2,
        terminal_marker={
            "sequence": "2",
            "attempt_id": "attempt-1",
            "event_name": "run_finish",
            "status": "completed",
        },
    )
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(
        StreamReplayGap,
        match="Redis closed before the database terminal watermark",
    ):
        stream.validate_cursor(
            "exe-1",
            after_sequence=cursor,
            committed_sequence=2,
            terminal_sequence=None,
            terminal_attempt_id=None,
            terminal_event_name=None,
            terminal_status=None,
        )


@pytest.mark.parametrize("cursor", [0, 2])
def test_replay_yields_nothing_and_stickily_degrades_when_redis_closes_before_db(
    monkeypatch: pytest.MonkeyPatch,
    cursor: int,
) -> None:
    import contentai.services.conversation_service as conversation_module
    from contentai.models.enums import RunStatus
    from contentai.services.conversation_service import ConversationService
    from contentai.services.errors import StreamReplayGapError

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
            agent_version_id=chat.agent_version_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            status=RunStatus.running,
            first_event_at=datetime(2026, 8, 4, 9, 0, tzinfo=UTC),
            stream_committed_sequence=2,
        )
        session.add(execution)
        session.commit()
        execution_id = execution.id

    redis = BoundedFakeRedis(
        earliest=1,
        latest=2,
        terminal_marker={
            "sequence": "2",
            "attempt_id": "attempt-1",
            "event_name": "run_finish",
            "status": "completed",
        },
    )
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    class FakeScope:
        def require_execution(self, **kwargs: Any) -> Any:
            return SimpleNamespace(execution=kwargs["session"].get(AgentExecution, execution_id))

    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    replay = service.replay_execution_events(
        get_engine(),
        execution_id,
        None,
        after_sequence=cursor,
        poll_interval_seconds=0.1,
    )

    with pytest.raises(StreamReplayGapError):
        next(replay)

    assert redis.read_args is None
    with Session(get_engine()) as session:
        degraded = session.get(AgentExecution, execution_id)
        assert degraded is not None
        assert degraded.streaming_degraded is True
        assert degraded.streaming_degraded_reason == "STREAM_REPLAY_GAP"
        first_degraded_at = degraded.streaming_degraded_at
        first_updated_at = degraded.updated_at

    ConversationService._mark_streaming_degraded(
        get_engine(),
        execution_id,
        "REDIS_READ_FAILED",
    )
    with Session(get_engine()) as session:
        degraded = session.get(AgentExecution, execution_id)
        assert degraded is not None
        assert degraded.streaming_degraded_reason == "STREAM_REPLAY_GAP"
        assert degraded.streaming_degraded_at == first_degraded_at
        assert degraded.updated_at == first_updated_at


def test_read_rejects_a_gap_inside_the_returned_batch() -> None:
    redis = FakeRedis()
    redis.read_response = [
        (
            execution_stream_key("exe-1"),
            [
                (
                    "9-0",
                    {
                        "sequence": "9",
                        "execution_id": "exe-1",
                        "type": "token",
                        "data": "{}",
                    },
                )
            ],
        )
    ]
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(StreamReplayGap, match="expected sequence 8, found 9"):
        stream.read("exe-1", after_sequence=7)


def test_empty_read_rechecks_expiration_after_redis_block() -> None:
    class ExpiringRedis(BoundedFakeRedis):
        def xread(self, *args: Any, **kwargs: Any) -> list[Any]:
            self.read_args = args
            self.read_kwargs = kwargs
            self.stream_exists = False
            return []

    redis = ExpiringRedis(earliest=1, latest=3)
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(StreamReplayExpired):
        stream.read(
            "exe-1",
            after_sequence=3,
            first_event_at=datetime(2026, 7, 13, 8, 0, tzinfo=UTC),
        )


def test_empty_read_rechecks_orphaned_sequence_key_without_database_marker() -> None:
    class ExpiringRedis(BoundedFakeRedis):
        def xread(self, *args: Any, **kwargs: Any) -> list[Any]:
            self.read_args = args
            self.read_kwargs = kwargs
            self.stream_exists = False
            return []

    redis = ExpiringRedis(earliest=1, latest=3)
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(StreamReplayExpired):
        stream.read("exe-1", after_sequence=3)


def test_empty_read_expires_after_this_connection_observed_history() -> None:
    class VanishingRedis(BoundedFakeRedis):
        def __init__(self) -> None:
            super().__init__(earliest=1, latest=1)
            self.read_count = 0

        def xread(self, *args: Any, **kwargs: Any) -> list[Any]:
            self.read_args = args
            self.read_kwargs = kwargs
            self.read_count += 1
            if self.read_count == 1:
                return [
                    (
                        execution_stream_key("exe-1"),
                        [
                            (
                                "1-0",
                                {
                                    "sequence": "1",
                                    "execution_id": "exe-1",
                                    "type": "token",
                                    "data": "{}",
                                },
                            )
                        ],
                    )
                ]
            self.earliest = None
            self.latest = None
            self.stream_exists = False
            self.sequence_exists = False
            return []

    redis = VanishingRedis()
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    assert [event.sequence for event in stream.read("exe-1", after_sequence=0)] == [1]
    with pytest.raises(StreamReplayExpired):
        stream.read(
            "exe-1",
            after_sequence=1,
            history_observed=True,
            validate_cursor=False,
        )


@pytest.mark.parametrize(
    ("stream_id", "field_sequence", "entry_execution_id"),
    [
        ("8-0", "not-an-integer", "exe-1"),
        ("8-0", None, "exe-1"),
        ("8-0", "0", "exe-1"),
        ("8-0", str(MAX_SAFE_EVENT_SEQUENCE + 1), "exe-1"),
        ("9-0", "8", "exe-1"),
        ("8-0", 8, "exe-1"),
        ("8-0", "8", "exe-other"),
    ],
)
def test_replay_rejects_noncanonical_entry_identity(
    stream_id: str,
    field_sequence: Any,
    entry_execution_id: str,
) -> None:
    redis = FakeRedis()
    fields: dict[str, Any] = {
        "execution_id": entry_execution_id,
        "type": "done",
        "data": "{}",
    }
    if field_sequence is not None:
        fields["sequence"] = field_sequence
    redis.read_response = [
        (
            execution_stream_key("exe-1"),
            [
                (
                    stream_id,
                    fields,
                )
            ],
        )
    ]
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)

    with pytest.raises(StreamReplayGap):
        stream.read("exe-1", after_sequence=7)


def test_replay_rejects_bad_interior_entry_before_yielding_valid_prefix() -> None:
    redis = FakeRedis()
    redis.read_response = [
        (
            execution_stream_key("exe-1"),
            [
                (
                    "1-0",
                    {
                        "sequence": "1",
                        "execution_id": "exe-1",
                        "type": "token",
                        "data": '{"content":"valid-prefix"}',
                    },
                ),
                (
                    "2-0",
                    {
                        "sequence": "2",
                        "execution_id": "exe-other",
                        "type": "token",
                        "data": '{"content":"must-not-yield"}',
                    },
                ),
            ],
        )
    ]
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)
    replay = stream.iter_events("exe-1", after_sequence=0)

    with pytest.raises(StreamReplayGap):
        next(replay)


def _seed_active_fenced_execution(execution_id: str) -> tuple[str, str]:
    worker_id = f"worker-{execution_id}"
    attempt_id = f"attempt-{execution_id}"
    with Session(get_engine()) as session:
        now = current_database_time(session)
        chat = ChatSession(
            id=f"session-{execution_id}",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            id=f"invocation-{execution_id}",
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(
            id=execution_id,
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
            status=RunStatus.running,
            worker_id=worker_id,
            attempt_count=1,
            claimed_at=now,
            heartbeat_at=now,
            lease_expires_at=now + timedelta(minutes=1),
        )
        session.add(execution)
        session.flush()
        session.add(
            AgentExecutionAttempt(
                id=attempt_id,
                execution_id=execution_id,
                ordinal=1,
                worker_id=worker_id,
            )
        )
        session.flush()
        execution.current_attempt_id = attempt_id
        session.add(execution)
        session.commit()
    return worker_id, attempt_id


class _CapturingSequencePublisher:
    def __init__(self) -> None:
        self.events: list[StreamEvent] = []

    def publish(self, events: list[StreamEvent]) -> None:
        self.events.extend(events)


def test_terminal_events_and_business_status_commit_in_one_database_transaction(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    execution_id = "execution-terminal-atomic-commit"
    old_event_time = datetime(2020, 1, 1, tzinfo=UTC)
    monkeypatch.setattr(runtime_events, "now_utc", lambda: old_event_time)
    worker_id, attempt_id = _seed_active_fenced_execution(execution_id)
    publisher = _CapturingSequencePublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=attempt_id,
    )
    writer.prepare_settlement()

    with Session(get_engine()) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        now = current_database_time(session)
        assert finish_current_attempt(
            session,
            execution,
            ExecutionAttemptStatus.completed,
            now=now,
            expected_worker_id=worker_id,
            expected_attempt_id=attempt_id,
            require_active_lease=True,
        )
        execution.status = RunStatus.completed
        execution.finished_at = now
        session.add(execution)
        writer.drain_settlement(
            [
                ("message_finish", {"execution_id": execution_id}),
                (
                    "attempt_end",
                    {
                        "execution_id": execution_id,
                        "attempt_id": attempt_id,
                        "status": "completed",
                    },
                ),
                ("run_finish", {"execution_id": execution_id}),
            ],
            db_session=session,
        )
        assert execution.stream_committed_sequence == 3
        assert execution.terminal_stream_sequence == 3
        assert execution.terminal_stream_attempt_id == attempt_id
        assert execution.terminal_stream_status == "completed"

        with Session(get_engine()) as observer:
            before_commit = observer.get(AgentExecution, execution_id)
            assert before_commit is not None
            assert before_commit.status == RunStatus.running
            assert before_commit.stream_committed_sequence == 0
            assert before_commit.terminal_stream_sequence is None

        session.commit()

    assert [event.sequence for event in publisher.events] == [1, 2, 3]
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        attempt = session.get(AgentExecutionAttempt, attempt_id)
        assert execution is not None
        assert execution.status == RunStatus.completed
        assert execution.terminal_stream_sequence == 3
        assert execution.first_event_at == old_event_time
        assert execution.updated_at >= execution.finished_at
        assert attempt is not None
        assert attempt.status == ExecutionAttemptStatus.completed
        assert attempt.finished_at is not None
        assert attempt.first_event_at == old_event_time


def test_resume_attempt_reopens_prior_waiting_terminal_and_closes_again() -> None:
    execution_id = "execution-waiting-resume-reopen"
    old_worker_id, old_attempt_id = _seed_active_fenced_execution(execution_id)
    new_worker_id = f"worker-resume-{execution_id}"
    new_attempt_id = f"attempt-resume-{execution_id}"
    with Session(get_engine()) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        old_attempt = session.get(AgentExecutionAttempt, old_attempt_id)
        assert old_attempt is not None
        now = current_database_time(session)
        old_attempt.status = ExecutionAttemptStatus.waiting_input
        old_attempt.finished_at = now
        session.add(old_attempt)
        session.add(
            AgentExecutionAttempt(
                id=new_attempt_id,
                execution_id=execution_id,
                ordinal=2,
                worker_id=new_worker_id,
            )
        )
        session.flush()
        execution.status = RunStatus.running
        execution.worker_id = new_worker_id
        execution.current_attempt_id = new_attempt_id
        execution.attempt_count = 2
        execution.claimed_at = now
        execution.heartbeat_at = now
        execution.lease_expires_at = now + timedelta(minutes=1)
        execution.first_event_at = now
        execution.stream_committed_sequence = 2
        execution.terminal_stream_sequence = 2
        execution.terminal_stream_attempt_id = old_attempt_id
        execution.terminal_stream_status = RunStatus.waiting_input.value
        session.add(execution)
        session.commit()

    publisher = _CapturingSequencePublisher()
    resumed_writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=new_worker_id,
        expected_attempt_id=new_attempt_id,
    )
    resumed_writer.emit("run_resume", {"execution_id": execution_id})

    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.stream_committed_sequence == 3
        assert execution.terminal_stream_sequence is None
        assert execution.terminal_stream_attempt_id is None
        assert execution.terminal_stream_status is None

    old_writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=old_worker_id,
        expected_attempt_id=old_attempt_id,
    )
    old_writer.emit(
        "assistant_message_delta",
        {"execution_id": execution_id, "message_id": "late", "chunk": "late"},
    )
    assert [event.sequence for event in publisher.events] == [3]

    resumed_writer.prepare_settlement()
    with Session(get_engine()) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        now = current_database_time(session)
        assert finish_current_attempt(
            session,
            execution,
            ExecutionAttemptStatus.waiting_input,
            now=now,
            expected_worker_id=new_worker_id,
            expected_attempt_id=new_attempt_id,
            require_active_lease=True,
        )
        execution.status = RunStatus.waiting_input
        execution.worker_id = None
        execution.claimed_at = None
        execution.heartbeat_at = None
        execution.lease_expires_at = None
        session.add(execution)
        resumed_writer.drain_settlement(
            [
                (
                    "attempt_end",
                    {
                        "execution_id": execution_id,
                        "attempt_id": new_attempt_id,
                        "status": "waiting_input",
                    },
                ),
                ("run_interrupt", {"execution_id": execution_id, "interrupt": {}}),
            ],
            db_session=session,
        )
        session.commit()

    assert [event.sequence for event in publisher.events] == [3, 4, 5]
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.waiting_input
        assert execution.stream_committed_sequence == 5
        assert execution.terminal_stream_sequence == 5
        assert execution.terminal_stream_attempt_id == new_attempt_id
        assert execution.terminal_stream_status == RunStatus.waiting_input.value


@pytest.mark.parametrize(
    ("terminal_status", "attempt_status", "terminal_event"),
    [
        (RunStatus.waiting_input, ExecutionAttemptStatus.waiting_input, "run_interrupt"),
        (RunStatus.completed, ExecutionAttemptStatus.completed, "run_finish"),
        (RunStatus.failed, ExecutionAttemptStatus.failed, "run_error"),
        (RunStatus.cancelled, ExecutionAttemptStatus.cancelled, "run_cancel"),
    ],
)
def test_terminal_settlement_drains_frozen_tail_once_and_rejects_late_tokens(
    terminal_status: RunStatus,
    attempt_status: ExecutionAttemptStatus,
    terminal_event: str,
) -> None:
    execution_id = f"execution-terminal-tail-{terminal_status.value}"
    worker_id, attempt_id = _seed_active_fenced_execution(execution_id)
    publisher = _CapturingSequencePublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=attempt_id,
    )
    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-tail", "chunk": "head", "done": False},
    )
    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-tail", "chunk": "tail", "done": False},
    )
    writer.prepare_settlement()
    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-tail", "chunk": "ignored", "done": False},
    )

    with Session(get_engine()) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        now = current_database_time(session)
        assert finish_current_attempt(
            session,
            execution,
            attempt_status,
            now=now,
            expected_worker_id=worker_id,
            expected_attempt_id=attempt_id,
            require_active_lease=True,
        )
        execution.status = terminal_status
        if terminal_status == RunStatus.waiting_input:
            execution.worker_id = None
            execution.claimed_at = None
            execution.heartbeat_at = None
            execution.lease_expires_at = None
        else:
            execution.finished_at = now
        session.add(execution)
        terminal_payload: dict[str, Any] = {"execution_id": execution_id}
        if terminal_event == "run_error":
            terminal_payload["error"] = "boom"
        elif terminal_event == "run_interrupt":
            terminal_payload["interrupt"] = {}
        writer.drain_settlement(
            [
                (
                    "attempt_end",
                    {
                        "execution_id": execution_id,
                        "attempt_id": attempt_id,
                        "status": terminal_status.value,
                    },
                ),
                (terminal_event, terminal_payload),
            ],
            db_session=session,
        )
        session.commit()

    writer.emit(
        "assistant_message_delta",
        {"message_id": "message-tail", "chunk": "late", "done": True},
    )
    token_contents = [
        str(event.payload.get("content") or "")
        for event in publisher.events
        if event.event_type == "token"
    ]
    assert token_contents == ["head", "tail"]
    assert [event.sequence for event in publisher.events] == [1, 2, 3, 4]
    assert publisher.events[-1].payload["name"] == terminal_event
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == terminal_status
        assert execution.stream_committed_sequence == 4
        assert execution.terminal_stream_sequence == 4
        assert execution.terminal_stream_attempt_id == attempt_id
        assert execution.terminal_stream_status == terminal_status.value


def test_cancel_request_drops_runtime_events_without_poisoning_terminal_drain() -> None:
    execution_id = "execution-cancel-request-event-phase"
    worker_id, attempt_id = _seed_active_fenced_execution(execution_id)
    publisher = _CapturingSequencePublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=attempt_id,
    )
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.cancel_requested_at = current_database_time(session)
        session.add(execution)
        session.commit()

    writer.emit(
        "assistant_message_delta",
        {"message_id": "cancelled-message", "chunk": "must-drop", "done": True},
    )
    assert publisher.events == []
    assert writer._fence_rejected is False

    writer.prepare_settlement()
    with Session(get_engine()) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        assert settle_execution_cancellation(
            session,
            execution,
            now=current_database_time(session),
            expected_worker_id=worker_id,
            expected_attempt_id=attempt_id,
            terminal_stream_will_publish=True,
        )
        writer.drain_settlement(
            [
                (
                    "attempt_end",
                    {
                        "execution_id": execution_id,
                        "attempt_id": attempt_id,
                        "status": "cancelled",
                    },
                ),
                ("run_cancel", {"execution_id": execution_id}),
            ],
            db_session=session,
        )
        session.commit()

    assert [event.payload["name"] for event in publisher.events] == [
        "attempt_end",
        "run_cancel",
    ]
    assert writer._fence_rejected is False


def test_started_delta_timer_cannot_publish_after_settlement_prepare() -> None:
    execution_id = "execution-terminal-timer-barrier"
    worker_id, attempt_id = _seed_active_fenced_execution(execution_id)
    publisher = _CapturingSequencePublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=attempt_id,
    )
    callback_started = threading.Event()

    def entered_timer_callback() -> None:
        callback_started.set()
        writer._flush_pending_delta_deadline()

    with writer._batch_lock:
        writer.emit(
            "assistant_message_delta",
            {"message_id": "timer-message", "chunk": "head", "done": False},
        )
        writer.emit(
            "assistant_message_delta",
            {"message_id": "timer-message", "chunk": "tail", "done": False},
        )
        scheduled_timer = writer._pending_delta_timer
        callback = threading.Thread(target=entered_timer_callback)
        callback.start()
        assert callback_started.wait(timeout=1)
        writer.prepare_settlement()
        assert writer._pending_delta is not None

    callback.join(timeout=1)
    assert not callback.is_alive()
    if scheduled_timer is not None:
        scheduled_timer.join(timeout=1)
        assert not scheduled_timer.is_alive()
    assert [event.payload.get("content") for event in publisher.events] == ["head"]

    with Session(get_engine()) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        now = current_database_time(session)
        assert finish_current_attempt(
            session,
            execution,
            ExecutionAttemptStatus.completed,
            now=now,
            expected_worker_id=worker_id,
            expected_attempt_id=attempt_id,
            require_active_lease=True,
        )
        execution.status = RunStatus.completed
        execution.finished_at = now
        session.add(execution)
        writer.drain_settlement(
            [
                (
                    "attempt_end",
                    {
                        "execution_id": execution_id,
                        "attempt_id": attempt_id,
                        "status": "completed",
                    },
                ),
                ("run_finish", {"execution_id": execution_id}),
            ],
            db_session=session,
        )
        session.commit()

    assert [
        event.payload.get("content")
        for event in publisher.events
        if event.event_type == "token"
    ] == ["head", "tail"]
    assert [event.sequence for event in publisher.events] == [1, 2, 3, 4]


@pytest.mark.parametrize("ownership_failure", ["worker-changed", "lease-expired"])
def test_terminal_drain_publishes_nothing_after_owner_authorization_fails(
    ownership_failure: str,
) -> None:
    execution_id = f"execution-terminal-owner-failure-{ownership_failure}"
    worker_id, attempt_id = _seed_active_fenced_execution(execution_id)
    publisher = _CapturingSequencePublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=attempt_id,
    )
    writer.emit(
        "assistant_message_delta",
        {"message_id": "owner-failure", "chunk": "head", "done": False},
    )
    writer.emit(
        "assistant_message_delta",
        {"message_id": "owner-failure", "chunk": "tail", "done": False},
    )
    writer.prepare_settlement()
    with Session(get_engine()) as session:
        if ownership_failure == "worker-changed":
            session.exec(
                text(
                    "UPDATE agentexecution SET worker_id = 'replacement-worker' "
                    "WHERE id = :execution_id"
                ),
                params={"execution_id": execution_id},
            )
        else:
            session.exec(
                text(
                    "UPDATE agentexecution "
                    "SET lease_expires_at = clock_timestamp() - interval '1 second' "
                    "WHERE id = :execution_id"
                ),
                params={"execution_id": execution_id},
            )
        session.commit()

    with Session(get_engine()) as session:
        with pytest.raises(EventStreamUnavailable):
            writer.drain_settlement(
                [
                    (
                        "attempt_end",
                        {
                            "execution_id": execution_id,
                            "attempt_id": attempt_id,
                            "status": "completed",
                        },
                    ),
                    ("run_finish", {"execution_id": execution_id}),
                ],
                db_session=session,
            )
        session.rollback()

    assert [event.sequence for event in publisher.events] == [1]
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.terminal_stream_sequence is None
        assert execution.stream_committed_sequence == 1


def test_terminal_tail_publish_failure_is_sticky_and_stops_official_events() -> None:
    execution_id = "execution-terminal-tail-publish-failure"
    worker_id, attempt_id = _seed_active_fenced_execution(execution_id)

    class FailOnSecondPublish:
        def __init__(self) -> None:
            self.calls = 0
            self.events: list[StreamEvent] = []

        def publish(self, events: list[StreamEvent]) -> None:
            self.calls += 1
            if self.calls == 2:
                raise EventStreamUnavailable("redis unavailable")
            self.events.extend(events)

    publisher = FailOnSecondPublish()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=attempt_id,
    )
    writer.emit(
        "assistant_message_delta",
        {"message_id": "failed-tail", "chunk": "head", "done": False},
    )
    writer.emit(
        "assistant_message_delta",
        {"message_id": "failed-tail", "chunk": "tail", "done": False},
    )
    writer.prepare_settlement()
    with Session(get_engine()) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        now = current_database_time(session)
        assert finish_current_attempt(
            session,
            execution,
            ExecutionAttemptStatus.completed,
            now=now,
            expected_worker_id=worker_id,
            expected_attempt_id=attempt_id,
            require_active_lease=True,
        )
        execution.status = RunStatus.completed
        execution.finished_at = now
        session.add(execution)
        with pytest.raises(EventStreamUnavailable):
            writer.drain_settlement(
                [
                    (
                        "attempt_end",
                        {
                            "execution_id": execution_id,
                            "attempt_id": attempt_id,
                            "status": "completed",
                        },
                    ),
                    ("run_finish", {"execution_id": execution_id}),
                ],
                db_session=session,
            )
        session.commit()

    assert publisher.calls == 2
    assert [event.sequence for event in publisher.events] == [1]
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.streaming_degraded is True
        assert execution.streaming_degraded_reason == "REDIS_PUBLISH_FAILED"
        assert execution.stream_committed_sequence == 1
        assert execution.terminal_stream_sequence is None


def test_memory_events_are_state_and_postprocess_writer_never_touches_main_timing() -> None:
    execution_id = "execution-postprocess-event-isolation"
    _seed_active_fenced_execution(execution_id)
    publisher = _CapturingSequencePublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        postprocess=True,
    )

    event_type, payload = writer._normalize_contract_event(
        "memory_extracted",
        {"execution_id": execution_id},
    )
    assert event_type == "state"
    assert payload["name"] == "memory_extracted"
    writer.emit("memory_extracted", {"execution_id": execution_id})

    assert publisher.events == []
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        attempt = session.get(AgentExecutionAttempt, execution.current_attempt_id)
        assert execution.first_event_at is None
        assert execution.first_token_at is None
        assert execution.stream_committed_sequence == 0
        assert attempt is not None
        assert attempt.first_event_at is None
        assert attempt.first_token_at is None


@pytest.mark.parametrize("after_first_event", [False, True])
def test_fenced_writer_refreshes_external_sticky_degradation_before_publish(
    after_first_event: bool,
) -> None:
    execution_id = f"execution-external-sticky-{after_first_event}"
    worker_id, attempt_id = _seed_active_fenced_execution(execution_id)
    publisher = _CapturingSequencePublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=attempt_id,
    )
    if after_first_event:
        writer.emit("run_start", {"execution_id": execution_id})
        assert [event.sequence for event in publisher.events] == [1]

    assert mark_streaming_degraded_first_wins(
        get_engine(),
        execution_id,
        "EXTERNAL_STICKY_REASON",
    )
    with Session(get_engine()) as session:
        before = session.get(AgentExecution, execution_id)
        assert before is not None
        first_event_at = before.first_event_at
        first_token_at = before.first_token_at
        degraded_at = before.streaming_degraded_at
        updated_at = before.updated_at

    writer.emit(
        "assistant_message_delta",
        {"message_id": "sticky", "chunk": "must-not-publish", "done": True},
    )
    writer.emit("tool_call_started", {"tool_name": "must-not-publish"})
    assert len(publisher.events) == (1 if after_first_event else 0)
    assert writer._streaming_degraded is True
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.streaming_degraded_reason == "EXTERNAL_STICKY_REASON"
        assert execution.streaming_degraded_at == degraded_at
        assert execution.updated_at == updated_at
        assert execution.first_event_at == first_event_at
        assert execution.first_token_at == first_token_at


def test_streaming_degradation_is_database_first_wins_under_concurrency() -> None:
    execution_id = "execution-degradation-first-wins-race"
    _seed_active_fenced_execution(execution_id)
    start = threading.Barrier(3)

    def mark(reason: str) -> tuple[str, bool]:
        start.wait(timeout=3)
        return reason, mark_streaming_degraded_first_wins(
            get_engine(),
            execution_id,
            reason,
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(mark, reason) for reason in ("FIRST", "SECOND")]
        start.wait(timeout=3)
        results = [future.result(timeout=3) for future in futures]

    winners = [reason for reason, changed in results if changed]
    assert len(winners) == 1
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.streaming_degraded_reason == winners[0]
        degraded_at = execution.streaming_degraded_at
        updated_at = execution.updated_at

    assert not mark_streaming_degraded_first_wins(
        get_engine(),
        execution_id,
        "LATE_OVERWRITE",
    )
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.streaming_degraded_reason == winners[0]
        assert execution.streaming_degraded_at == degraded_at
        assert execution.updated_at == updated_at


def test_old_attempt_cannot_mark_new_attempt_streaming_degraded() -> None:
    execution_id = "execution-old-attempt-degradation-fence"
    worker_id, old_attempt_id = _seed_active_fenced_execution(execution_id)
    new_attempt_id = f"attempt-new-{execution_id}"
    with Session(get_engine()) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        session.add(
            AgentExecutionAttempt(
                id=new_attempt_id,
                execution_id=execution_id,
                ordinal=2,
                worker_id=worker_id,
            )
        )
        session.flush()
        execution.current_attempt_id = new_attempt_id
        execution.attempt_count = 2
        session.add(execution)
        session.commit()

    assert not mark_streaming_degraded_first_wins(
        get_engine(),
        execution_id,
        "STALE_WRITER_REASON",
        expected_worker_id=worker_id,
        expected_attempt_id=old_attempt_id,
    )
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.streaming_degraded is False
        assert execution.streaming_degraded_reason == ""


@pytest.mark.parametrize(
    "display_clock",
    [datetime(1990, 1, 1, tzinfo=UTC), datetime(2090, 1, 1, tzinfo=UTC)],
)
def test_event_authorization_uses_fresh_db_clock_after_lock_wait(
    monkeypatch: pytest.MonkeyPatch,
    display_clock: datetime,
) -> None:
    execution_id = f"execution-event-db-clock-{display_clock.year}"
    worker_id, attempt_id = _seed_active_fenced_execution(execution_id)
    engine = get_engine()
    with Session(engine) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        lease_expires_at = current_database_time(session) + timedelta(milliseconds=250)
        execution.lease_expires_at = lease_expires_at
        session.add(execution)
        session.commit()

    holder = Session(engine)
    holder.exec(
        select(AgentExecution)
        .where(AgentExecution.id == execution_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).one()
    writer_select_started = threading.Event()

    def observe_writer_select(
        _connection: Any,
        _cursor: Any,
        _statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        if threading.current_thread().name == "event-expiry-writer":
            writer_select_started.set()

    publisher = _CapturingSequencePublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        engine,
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=attempt_id,
    )
    monkeypatch.setattr(runtime_events, "now_utc", lambda: display_clock)
    sqlalchemy_event.listen(engine, "before_cursor_execute", observe_writer_select)
    writer_errors: list[BaseException] = []

    def write_event() -> None:
        try:
            writer.emit("run_start", {"execution_id": execution_id})
        except BaseException as exc:  # noqa: BLE001
            writer_errors.append(exc)

    writer_thread = threading.Thread(target=write_event, name="event-expiry-writer")
    writer_thread.start()
    try:
        assert writer_select_started.wait(timeout=3)
        writer_thread.join(timeout=0.1)
        assert writer_thread.is_alive()
        with Session(engine) as clock_session:
            while current_database_time(clock_session) <= lease_expires_at:
                pass
    finally:
        holder.rollback()
        writer_thread.join(timeout=3)
        sqlalchemy_event.remove(engine, "before_cursor_execute", observe_writer_select)
        holder.close()

    assert not writer_thread.is_alive()
    assert writer_errors == []
    assert publisher.events == []
    assert writer._fence_rejected is True
    with Session(engine) as session:
        execution = session.get(AgentExecution, execution_id)
        attempt = session.get(AgentExecutionAttempt, attempt_id)
        assert execution is not None
        assert execution.stream_committed_sequence == 0
        assert execution.first_event_at is None
        assert execution.first_token_at is None
        assert attempt is not None
        assert attempt.first_event_at is None
        assert attempt.first_token_at is None


def test_redis_ahead_after_terminal_database_rollback_degrades_before_yield() -> None:
    from contentai.services.conversation_service import ConversationService

    execution_id = "execution-terminal-rollback-watermark"
    worker_id, attempt_id = _seed_active_fenced_execution(execution_id)
    publisher = _CapturingSequencePublisher()
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
        expected_worker_id=worker_id,
        expected_attempt_id=attempt_id,
    )
    writer.prepare_settlement()

    with Session(get_engine()) as session:
        execution = session.exec(
            select(AgentExecution)
            .where(AgentExecution.id == execution_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).one()
        now = current_database_time(session)
        assert finish_current_attempt(
            session,
            execution,
            ExecutionAttemptStatus.failed,
            now=now,
            expected_worker_id=worker_id,
            expected_attempt_id=attempt_id,
            require_active_lease=True,
        )
        execution.status = RunStatus.failed
        execution.finished_at = now
        session.add(execution)
        writer.drain_settlement(
            [
                (
                    "attempt_end",
                    {
                        "execution_id": execution_id,
                        "attempt_id": attempt_id,
                        "status": "failed",
                    },
                ),
                ("run_error", {"execution_id": execution_id, "error": "boom"}),
            ],
            db_session=session,
        )
        assert execution.stream_committed_sequence == 2
        session.rollback()

    assert [event.sequence for event in publisher.events] == [1, 2]
    with pytest.raises(StreamReplayGap, match="before its database watermark committed"):
        ConversationService._validate_replay_visibility(
            get_engine(),
            execution_id,
            realtime_stream=SimpleNamespace(),
            rows=publisher.events,
            cursor=0,
            history_observed=False,
        )

    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        attempt = session.get(AgentExecutionAttempt, attempt_id)
        assert execution is not None
        assert execution.status == RunStatus.running
        assert execution.stream_committed_sequence == 0
        assert execution.terminal_stream_sequence is None
        assert execution.streaming_degraded is True
        assert execution.streaming_degraded_reason == "STREAM_COMMIT_WATERMARK_MISMATCH"
        assert attempt is not None
        assert attempt.status == ExecutionAttemptStatus.running


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


def test_event_timing_persistence_failure_stickily_degrades_and_stops_publishing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamp = datetime(2026, 8, 4, 9, 30, tzinfo=UTC)
    execution_id = "execution-event-timing-persistence-failure"
    _seed_active_fenced_execution(execution_id)
    original_touch_updated_at = AgentExecution.touch_updated_at
    touch_calls = 0

    def fail_first_touch(execution: AgentExecution, value: datetime | None = None) -> None:
        nonlocal touch_calls
        touch_calls += 1
        if touch_calls == 1:
            raise RuntimeError("event timing persistence failed")
        original_touch_updated_at(execution, value)

    class CapturingPublisher:
        def __init__(self) -> None:
            self.events: list[StreamEvent] = []

        def publish(self, events: list[StreamEvent]) -> None:
            self.events.extend(events)

    publisher = CapturingPublisher()
    monkeypatch.setattr(runtime_events, "now_utc", lambda: timestamp)
    monkeypatch.setattr(AgentExecution, "touch_updated_at", fail_first_touch)
    writer = PersistentAgentEventWriter(
        execution_id,
        get_engine(),
        stream_publisher=publisher,
    )

    writer.emit("state", {"status": "running"})
    writer.emit("state", {"status": "still-running"})

    assert [event.sequence for event in publisher.events] == [1]
    assert writer._streaming_degraded is True
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.streaming_degraded is True
        assert execution.streaming_degraded_at is not None
        assert execution.streaming_degraded_reason == "STREAM_COMMIT_WATERMARK_MISMATCH"
        assert execution.stream_committed_sequence == 0
        assert execution.first_event_at is None
    assert touch_calls == 2


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


@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        ("EVENT_TIMING_PERSIST_FAILED", "EVENT_TIMING_PERSIST_FAILED"),
        ("", "STREAMING_DEGRADED"),
    ],
)
def test_validate_execution_event_cursor_rejects_sticky_degradation_before_redis(
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
    expected: str,
) -> None:
    import contentai.services.conversation_service as conversation_module
    from contentai.services.conversation_service import ConversationService
    from contentai.services.errors import StreamingDegradedError

    redis_calls: list[object] = []
    execution_id = f"execution-sticky-degraded-{expected.lower()}"
    _seed_active_fenced_execution(execution_id)
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.streaming_degraded = True
        execution.streaming_degraded_reason = reason
        session.add(execution)
        session.commit()

    class FakeScope:
        def require_execution(self, **_kwargs):
            return SimpleNamespace(execution=SimpleNamespace(id=execution_id))

    def create_stream(settings):
        redis_calls.append(settings)
        raise AssertionError("sticky degradation must short-circuit Redis")

    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=create_stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())

    with Session(get_engine()) as session:
        with pytest.raises(StreamingDegradedError, match=expected):
            service.validate_execution_event_cursor(
                session,
                execution_id,
                None,
                after_sequence=0,
            )

    assert redis_calls == []


def test_future_cursor_stickily_degrades_a_server_committed_watermark_gap(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contentai.services.conversation_service as conversation_module
    from contentai.services.conversation_service import ConversationService
    from contentai.services.errors import StreamReplayGapError

    execution_id = "execution-future-cursor-server-watermark-gap"
    _seed_active_fenced_execution(execution_id)
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.first_event_at = datetime(2026, 8, 4, 9, 0, tzinfo=UTC)
        execution.stream_committed_sequence = 1
        session.add(execution)
        session.commit()

    stream = RedisEventStream(
        BoundedFakeRedis(earliest=1, latest=2),
        ttl_seconds=60,
        max_length=100,
    )

    class FakeScope:
        def require_execution(self, **kwargs: Any) -> Any:
            return SimpleNamespace(
                execution=kwargs["session"].get(AgentExecution, execution_id)
            )

    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())

    with Session(get_engine()) as session:
        with pytest.raises(
            StreamReplayGapError,
            match="Redis and database committed watermarks disagree",
        ):
            service.validate_execution_event_cursor(
                session,
                execution_id,
                None,
                after_sequence=3,
            )

    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.streaming_degraded is True
        assert execution.streaming_degraded_reason == "STREAM_REPLAY_GAP"


def test_execution_event_replay_stickily_degrades_on_invalid_legacy_payload(monkeypatch):
    import contentai.services.conversation_service as conversation_module
    from contentai.services.conversation_service import ConversationService
    from contentai.services.errors import StreamingDegradedError

    execution = _fake_replay_execution()
    redis = FakeRedis()
    redis.read_response = [
        (
            execution_stream_key("execution-1"),
            [
                (
                    "1-0",
                    {
                        "sequence": "1",
                        "execution_id": "execution-1",
                        "type": "token",
                        "data": '{"value":NaN}',
                    },
                )
            ],
        )
    ]
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)
    marked_degraded: list[tuple[object, str, str]] = []

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

    session_bind = object()
    monkeypatch.setattr(conversation_module, "Session", FakeSession)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        service,
        "_mark_streaming_degraded",
        lambda bind, execution_id, reason: marked_degraded.append(
            (bind, execution_id, reason)
        ),
    )
    replay = service.replay_execution_events(
        session_bind,
        "execution-1",
        None,
        poll_interval_seconds=0.1,
    )

    with pytest.raises(StreamingDegradedError, match="REDIS_READ_FAILED"):
        next(replay)

    assert marked_degraded == [(session_bind, "execution-1", "REDIS_READ_FAILED")]


def test_execution_event_replay_stickily_degrades_on_bad_interior_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import contentai.services.conversation_service as conversation_module
    from contentai.services.conversation_service import ConversationService
    from contentai.services.errors import StreamReplayGapError

    execution = _fake_replay_execution()
    redis = FakeRedis()
    redis.read_response = [
        (
            execution_stream_key("execution-1"),
            [
                (
                    "1-0",
                    {
                        "sequence": "1",
                        "execution_id": "execution-1",
                        "type": "token",
                        "data": '{"content":"valid-prefix"}',
                    },
                ),
                (
                    "2-0",
                    {
                        "sequence": "2",
                        "execution_id": "execution-other",
                        "type": "token",
                        "data": '{"content":"must-not-yield"}',
                    },
                ),
            ],
        )
    ]
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)
    marked_degraded: list[tuple[object, str, str]] = []

    class FakeScope:
        def require_execution(self, **_kwargs: Any) -> SimpleNamespace:
            return SimpleNamespace(execution=execution)

    class FakeSession:
        def __init__(self, _bind: Any) -> None:
            pass

        def __enter__(self) -> FakeSession:
            return self

        def __exit__(self, *_args: Any) -> None:
            return None

        def get(self, _model: Any, _identifier: Any) -> Any:
            return execution

    session_bind = object()
    monkeypatch.setattr(conversation_module, "Session", FakeSession)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        service,
        "_mark_streaming_degraded",
        lambda bind, execution_id, reason: marked_degraded.append(
            (bind, execution_id, reason)
        ),
    )
    replay = service.replay_execution_events(
        session_bind,
        "execution-1",
        None,
        poll_interval_seconds=0.1,
    )

    with pytest.raises(StreamReplayGapError):
        next(replay)

    assert marked_degraded == [(session_bind, "execution-1", "STREAM_REPLAY_GAP")]


@pytest.mark.parametrize("entrypoint", ["replay", "preflight"])
@pytest.mark.parametrize("error_kind", ["gap", "expired", "unavailable"])
@pytest.mark.parametrize("persistence_failure", ["get", "commit", "rollback"])
def test_stream_error_code_survives_degradation_persistence_failure(
    monkeypatch,
    entrypoint,
    error_kind,
    persistence_failure,
):
    import contentai.services.conversation_service as conversation_module
    from contentai.services.conversation_service import ConversationService
    from contentai.services.errors import (
        StreamingDegradedError,
        StreamReplayExpiredError,
        StreamReplayGapError,
    )

    if error_kind == "gap":
        stream_error = StreamReplayGap("stable gap")
        expected_error = StreamReplayGapError
        expected_message = "stable gap"
        expected_reason = "STREAM_REPLAY_GAP"
    elif error_kind == "expired":
        stream_error = StreamReplayExpired("stable expired")
        expected_error = StreamReplayExpiredError
        expected_message = "stable expired"
        expected_reason = "STREAM_REPLAY_EXPIRED"
    else:
        stream_error = EventStreamUnavailable("unstable infrastructure detail")
        expected_error = StreamingDegradedError
        expected_message = "REDIS_READ_FAILED"
        expected_reason = "REDIS_READ_FAILED"

    execution = _fake_replay_execution()

    class FakeScope:
        def require_execution(self, **_kwargs):
            return SimpleNamespace(execution=execution)

    class InitialSession:
        def __init__(self, _bind):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class RequestSession:
        def __init__(self) -> None:
            self.exec_calls = 0
            self.rollback_calls = 0

        def exec(self, _statement):
            self.exec_calls += 1
            if self.exec_calls == 1:
                return _SingleResult(execution)
            if persistence_failure in {"get", "rollback"}:
                raise RuntimeError("degradation clock lookup failed")
            return _SingleResult(datetime.now(UTC))

        def add(self, _value) -> None:
            return None

        def commit(self) -> None:
            if persistence_failure == "commit":
                raise RuntimeError("degradation commit failed")

        def rollback(self) -> None:
            self.rollback_calls += 1
            if persistence_failure == "rollback":
                raise RuntimeError("degradation rollback failed")

    class FailingStream:
        def read(self, _execution_id, **_kwargs):
            raise stream_error

        def validate_cursor(self, _execution_id, **_kwargs):
            raise stream_error

    session_bind = object()
    stream = FailingStream()
    monkeypatch.setattr(conversation_module, "Session", InitialSession)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: execution,
    )
    replay_mark_reasons: list[str] = []

    def fail_independent_mark(_bind, _execution_id, reason: str) -> None:
        replay_mark_reasons.append(reason)
        raise RuntimeError(f"degradation {persistence_failure} failed")

    monkeypatch.setattr(
        conversation_module,
        "mark_streaming_degraded_first_wins",
        fail_independent_mark,
    )

    request_session = RequestSession()
    with pytest.raises(expected_error) as raised:
        if entrypoint == "replay":
            replay = service.replay_execution_events(
                session_bind,
                "execution-1",
                None,
                poll_interval_seconds=0.1,
            )
            next(replay)
        else:
            service.validate_execution_event_cursor(
                request_session,
                "execution-1",
                None,
                after_sequence=0,
            )

    assert str(raised.value) == expected_message
    if entrypoint == "replay":
        assert replay_mark_reasons == [expected_reason]
    else:
        assert replay_mark_reasons == []
        assert request_session.rollback_calls == 1
        if persistence_failure == "commit":
            assert execution.streaming_degraded_reason == expected_reason


def test_execution_event_replay_validates_the_redis_cursor_only_once(monkeypatch):
    import contentai.services.conversation_service as conversation_module
    from contentai.models.enums import RunStatus
    from contentai.services.conversation_service import ConversationService

    execution = _fake_replay_execution(stream_committed_sequence=2)
    read_calls: list[tuple[bool, bool]] = []

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
            read_calls.append(
                (kwargs["validate_cursor"], kwargs["history_observed"])
            )
            batch = self.batches.pop(0)
            if batch[0].sequence == 2:
                execution.status = RunStatus.completed
                execution.terminal_stream_sequence = 2
                execution.terminal_stream_attempt_id = execution.current_attempt_id
                execution.terminal_stream_status = RunStatus.completed.value
            return batch

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
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        service,
        "_validate_replay_visibility",
        lambda *_args, **_kwargs: execution,
    )

    events = list(
        service.replay_execution_events(
            object(), "execution-1", None, poll_interval_seconds=0.1
        )
    )

    assert [event_name for event_name, _payload in events] == ["token", "done"]
    assert read_calls == [(True, False), (False, True)]


def test_execution_event_replay_translates_expiry_after_observed_history(monkeypatch):
    import contentai.services.conversation_service as conversation_module
    from contentai.services.conversation_service import ConversationService
    from contentai.services.errors import StreamReplayExpiredError

    execution = _fake_replay_execution(stream_committed_sequence=1)
    read_calls: list[tuple[bool, bool]] = []
    degraded: list[tuple[object, str, str]] = []

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
        def read(self, _execution_id, **kwargs):
            call = (kwargs["validate_cursor"], kwargs["history_observed"])
            read_calls.append(call)
            if len(read_calls) == 1:
                return [
                    StreamEvent(
                        1,
                        "execution-1",
                        "token",
                        datetime.now(UTC),
                        {"content": "first"},
                    )
                ]
            assert call == (False, True)
            raise StreamReplayExpired("expired after observed history")

    stream = FakeStream()
    session_bind = object()
    monkeypatch.setattr(conversation_module, "Session", FakeSession)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        service,
        "_validate_replay_visibility",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        service,
        "_mark_streaming_degraded",
        lambda bind, scoped_execution_id, reason: degraded.append(
            (bind, scoped_execution_id, reason)
        ),
    )
    events = service.replay_execution_events(
        session_bind,
        "execution-1",
        None,
        poll_interval_seconds=0.1,
    )

    event_name, payload = next(events)
    assert event_name == "token"
    assert payload["sequence"] == 1
    with pytest.raises(StreamReplayExpiredError, match="expired after observed history"):
        next(events)

    assert read_calls == [(True, False), (False, True)]
    assert degraded == [(session_bind, "execution-1", "STREAM_REPLAY_EXPIRED")]


def test_execution_event_replay_refreshes_late_marker_before_terminal_drain(monkeypatch):
    import contentai.services.conversation_service as conversation_module
    from contentai.models.enums import RunStatus
    from contentai.services.conversation_service import ConversationService
    from contentai.services.errors import StreamReplayExpiredError

    marker = datetime(2026, 8, 4, 10, 0, tzinfo=UTC)
    initial_execution = _fake_replay_execution()
    refreshed_execution = _fake_replay_execution(
        first_event_at=marker,
        status=RunStatus.completed,
    )
    degraded: list[tuple[object, str, str]] = []

    class FakeScope:
        def require_execution(self, **_kwargs):
            return SimpleNamespace(execution=initial_execution)

    class FakeSession:
        def __init__(self, _bind):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _identifier):
            return refreshed_execution

    class MissingRedis(BoundedFakeRedis):
        def __init__(self) -> None:
            super().__init__(
                earliest=None,
                latest=None,
                stream_exists=False,
                sequence_exists=False,
            )
            self.read_count = 0

        def xread(self, *args: Any, **kwargs: Any) -> list[Any]:
            self.read_args = args
            self.read_kwargs = kwargs
            self.read_count += 1
            return []

    redis = MissingRedis()
    stream = RedisEventStream(redis, ttl_seconds=60, max_length=100)
    clock = [0.0]

    def advance_clock() -> float:
        clock[0] += 0.2
        return clock[0]

    session_bind = object()
    monkeypatch.setattr(conversation_module, "Session", FakeSession)
    monkeypatch.setattr(conversation_module.time, "monotonic", advance_clock)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    snapshots = iter((initial_execution, refreshed_execution))
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )
    monkeypatch.setattr(
        service,
        "_validate_replay_visibility",
        lambda *_args, **_kwargs: initial_execution,
    )
    monkeypatch.setattr(
        service,
        "_mark_streaming_degraded",
        lambda bind, scoped_execution_id, reason: degraded.append(
            (bind, scoped_execution_id, reason)
        ),
    )
    events = service.replay_execution_events(
        session_bind,
        "execution-1",
        None,
        poll_interval_seconds=0.1,
    )

    with pytest.raises(StreamReplayExpiredError):
        next(events)

    assert redis.read_count == 2
    assert degraded == [(session_bind, "execution-1", "STREAM_REPLAY_EXPIRED")]


def test_execution_event_replay_raises_late_degradation_before_terminal_drain(monkeypatch):
    import contentai.services.conversation_service as conversation_module
    from contentai.models.enums import RunStatus
    from contentai.services.conversation_service import ConversationService
    from contentai.services.errors import StreamingDegradedError

    initial_execution = _fake_replay_execution()
    refreshed_execution = _fake_replay_execution(
        streaming_degraded=True,
        streaming_degraded_reason="EVENT_TIMING_PERSIST_FAILED",
        status=RunStatus.completed,
    )

    class FakeScope:
        def require_execution(self, **_kwargs):
            return SimpleNamespace(execution=initial_execution)

    class FakeSession:
        def __init__(self, _bind):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _identifier):
            return refreshed_execution

    class EmptyStream:
        def __init__(self) -> None:
            self.read_count = 0

        def read(self, _execution_id, **_kwargs):
            self.read_count += 1
            return []

    stream = EmptyStream()
    clock = [0.0]

    def advance_clock() -> float:
        clock[0] += 0.2
        return clock[0]

    monkeypatch.setattr(conversation_module, "Session", FakeSession)
    monkeypatch.setattr(conversation_module.time, "monotonic", advance_clock)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    snapshots = iter((initial_execution, refreshed_execution))
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )
    monkeypatch.setattr(
        service,
        "_validate_replay_visibility",
        lambda *_args, **_kwargs: initial_execution,
    )
    events = service.replay_execution_events(
        object(),
        "execution-1",
        None,
        poll_interval_seconds=0.1,
    )

    with pytest.raises(StreamingDegradedError, match="EVENT_TIMING_PERSIST_FAILED"):
        next(events)

    assert stream.read_count == 1


def test_execution_event_replay_final_drain_yields_terminal_row_after_empty_read(monkeypatch):
    import contentai.services.conversation_service as conversation_module
    from contentai.models.enums import RunStatus
    from contentai.services.conversation_service import ConversationService

    initial_execution = _fake_replay_execution()
    refreshed_execution = _fake_replay_execution(
        status=RunStatus.completed,
        stream_committed_sequence=1,
        terminal_stream_sequence=1,
    )
    read_calls: list[tuple[bool, bool]] = []

    class FakeScope:
        def require_execution(self, **_kwargs):
            return SimpleNamespace(execution=initial_execution)

    class FakeSession:
        def __init__(self, _bind):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def get(self, _model, _identifier):
            return refreshed_execution

    class AppearingTerminalStream:
        def read(self, _execution_id, **kwargs):
            call = (kwargs["validate_cursor"], kwargs["history_observed"])
            read_calls.append(call)
            if len(read_calls) == 1:
                return []
            return [
                StreamEvent(
                    1,
                    "execution-1",
                    "done",
                    datetime.now(UTC),
                    {"status": "completed"},
                )
            ]

    stream = AppearingTerminalStream()
    clock = [0.0]

    def advance_clock() -> float:
        clock[0] += 0.2
        return clock[0]

    monkeypatch.setattr(conversation_module, "Session", FakeSession)
    monkeypatch.setattr(conversation_module.time, "monotonic", advance_clock)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: stream),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    snapshots = iter((initial_execution, refreshed_execution))
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: next(snapshots),
    )
    monkeypatch.setattr(
        service,
        "_validate_replay_visibility",
        lambda *_args, rows, **_kwargs: (
            refreshed_execution if rows else initial_execution
        ),
    )

    events = list(
        service.replay_execution_events(
            object(),
            "execution-1",
            None,
            poll_interval_seconds=0.1,
        )
    )

    assert [event_name for event_name, _payload in events] == ["done"]
    assert events[0][1]["sequence"] == 1
    assert read_calls == [(True, False), (False, False)]


@pytest.mark.parametrize("late_evidence", ["none", "marker", "degraded"])
def test_terminal_snapshot_without_watermark_is_sticky_and_bounded(
    late_evidence: str,
) -> None:
    from contentai.services.conversation_service import ConversationService

    marker = datetime(2026, 8, 4, 10, 30, tzinfo=UTC)
    execution_id = f"execution-terminal-missing-watermark-{late_evidence}"
    _seed_active_fenced_execution(execution_id)
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.status = RunStatus.completed
        execution.first_event_at = marker if late_evidence == "marker" else None
        if late_evidence == "degraded":
            now = current_database_time(session)
            execution.streaming_degraded = True
            execution.streaming_degraded_at = now
            execution.streaming_degraded_reason = "EVENT_TIMING_PERSIST_FAILED"
        session.add(execution)
        session.commit()

    snapshot = ConversationService._load_replay_snapshot(get_engine(), execution_id)
    assert snapshot is not None
    assert snapshot.streaming_degraded is True
    expected_reason = (
        "EVENT_TIMING_PERSIST_FAILED"
        if late_evidence == "degraded"
        else "TERMINAL_STREAM_NOT_PUBLISHED"
    )
    assert snapshot.streaming_degraded_reason == expected_reason
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.streaming_degraded_reason == expected_reason


@pytest.mark.parametrize("new_attempt", [False, True])
def test_prior_waiting_terminal_watermark_survives_resume_transition(
    new_attempt: bool,
) -> None:
    from contentai.services.conversation_service import ConversationService

    execution_id = f"execution-prior-waiting-terminal-{int(new_attempt)}"
    _worker_id, waiting_attempt_id = _seed_active_fenced_execution(execution_id)
    current_attempt_id = waiting_attempt_id
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        if new_attempt:
            current_attempt_id = f"attempt-resume-{execution_id}"
            session.add(
                AgentExecutionAttempt(
                    id=current_attempt_id,
                    execution_id=execution_id,
                    ordinal=2,
                    worker_id=execution.worker_id or "worker-resume",
                )
            )
            session.flush()
            execution.current_attempt_id = current_attempt_id
            execution.attempt_count = 2
        execution.status = RunStatus.pending
        execution.stream_committed_sequence = 1
        execution.terminal_stream_sequence = 1
        execution.terminal_stream_attempt_id = waiting_attempt_id
        execution.terminal_stream_status = RunStatus.waiting_input.value
        session.add(execution)
        session.commit()

    snapshot = ConversationService._load_replay_snapshot(get_engine(), execution_id)
    assert snapshot is not None
    assert snapshot.streaming_degraded is False
    assert snapshot.current_attempt_id == current_attempt_id
    assert snapshot.terminal_stream_attempt_id == waiting_attempt_id
    assert snapshot.terminal_stream_status == RunStatus.waiting_input.value


def test_execution_event_replay_overrides_payload_execution_id_with_scope(monkeypatch):
    import contentai.services.conversation_service as conversation_module
    from contentai.models.enums import RunStatus
    from contentai.services.conversation_service import ConversationService

    execution = _fake_replay_execution(
        status=RunStatus.completed,
        stream_committed_sequence=1,
        terminal_stream_sequence=1,
    )

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
        def read(self, _execution_id, **_kwargs):
            return [
                StreamEvent(
                    1,
                    "forged-execution",
                    "done",
                    datetime.now(UTC),
                    {"execution_id": "forged-execution"},
                )
            ]

    monkeypatch.setattr(conversation_module, "Session", FakeSession)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: FakeStream()),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        service,
        "_validate_replay_visibility",
        lambda *_args, **_kwargs: execution,
    )

    events = list(
        service.replay_execution_events(
            object(), "execution-1", None, poll_interval_seconds=0.1
        )
    )

    assert events[0][1]["execution_id"] == "execution-1"


def test_execution_event_replay_stops_after_client_disconnect(monkeypatch):
    import contentai.services.conversation_service as conversation_module
    from contentai.models.enums import RunStatus
    from contentai.services.conversation_service import ConversationService

    execution = _fake_replay_execution(
        status=RunStatus.waiting_input,
        stream_committed_sequence=1,
        terminal_stream_sequence=1,
    )
    read_started = threading.Event()
    stop_requested = threading.Event()

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
        def read(self, _execution_id, **_kwargs):
            read_started.set()
            return []

    monkeypatch.setattr(conversation_module, "Session", FakeSession)
    monkeypatch.setattr(
        conversation_module,
        "RedisEventStream",
        SimpleNamespace(from_settings=lambda _settings: FakeStream()),
    )
    service = object.__new__(ConversationService)
    service._execution_scope_guard = FakeScope()
    service.agent_service = SimpleNamespace(settings=SimpleNamespace())
    monkeypatch.setattr(
        service,
        "_load_replay_snapshot",
        lambda *_args, **_kwargs: execution,
    )
    monkeypatch.setattr(
        service,
        "_validate_replay_visibility",
        lambda *_args, **_kwargs: execution,
    )
    consumer = threading.Thread(
        target=lambda: list(
            service.replay_execution_events(
                object(),
                "execution-1",
                None,
                poll_interval_seconds=5,
                stop_requested=stop_requested,
            )
        )
    )

    consumer.start()
    assert read_started.wait(timeout=1)
    stop_requested.set()
    consumer.join(timeout=1)

    assert not consumer.is_alive()


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
