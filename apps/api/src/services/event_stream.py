from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)
EXECUTION_STREAM_PREFIX = "contentai:execution"
_STREAM_INSTANCES: dict[tuple[str, int, int, int], RedisEventStream] = {}

# XADD deliberately precedes SET. Lua is atomic, so an XADD failure cannot
# consume a sequence that was never committed to the stream.
_PUBLISH_EVENT_LUA = """
local current = tonumber(redis.call('GET', KEYS[2]) or '0')
if not current then return redis.error_reply('invalid execution sequence') end
local next_sequence = current + 1
local stream_id = tostring(next_sequence) .. '-0'
redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[1], stream_id,
  'sequence', tostring(next_sequence), 'execution_id', ARGV[2],
  'type', ARGV[3], 'timestamp', ARGV[4], 'data', ARGV[5])
redis.call('SET', KEYS[2], tostring(next_sequence), 'EX', ARGV[6])
redis.call('EXPIRE', KEYS[1], ARGV[6])
return next_sequence
"""


class EventStreamUnavailable(RuntimeError):
    """Redis, the only live-event transport, could not accept an event."""


class StreamReplayGap(RuntimeError):
    pass


class StreamReplayExpired(RuntimeError):
    pass


class InvalidStreamCursor(RuntimeError):
    pass


class EventStreamPublisher(Protocol):
    def publish(self, events: Sequence[StreamEvent]) -> None: ...


@dataclass(frozen=True)
class StreamEvent:
    sequence: int
    execution_id: str
    event_type: str
    timestamp: datetime | None
    payload: dict[str, Any]


@dataclass(frozen=True)
class StreamBounds:
    earliest_sequence: int | None
    latest_sequence: int | None
    stream_exists: bool
    sequence_exists: bool


def execution_stream_key(execution_id: str) -> str:
    return f"{EXECUTION_STREAM_PREFIX}:{execution_id}:events"


def execution_sequence_key(execution_id: str) -> str:
    return f"{EXECUTION_STREAM_PREFIX}:{execution_id}:sequence"


def redis_stream_id(sequence: int) -> str:
    return f"{max(0, int(sequence))}-0"


class RedisEventStream:
    """Bounded, per-execution Redis stream with no process-local fallback."""

    def __init__(
        self,
        redis: Redis,
        *,
        ttl_seconds: int,
        max_length: int,
        block_ms: int = 5_000,
        failure_cooldown_seconds: float | None = None,
    ) -> None:
        self.redis = redis
        self.ttl_seconds = max(1, int(ttl_seconds))
        self.max_length = max(1, int(max_length))
        self.block_ms = max(1, int(block_ms))

    @classmethod
    def from_settings(cls, settings: Any) -> RedisEventStream:
        config = settings.redis
        cache_key = (
            config.url,
            config.event_ttl_seconds,
            config.event_max_length,
            config.event_block_ms,
        )
        if cached := _STREAM_INSTANCES.get(cache_key):
            return cached
        instance = cls(
            Redis.from_url(
                config.url,
                decode_responses=True,
                socket_connect_timeout=0.5,
                socket_timeout=max(1.0, config.event_block_ms / 1000 + 0.5),
            ),
            ttl_seconds=config.event_ttl_seconds,
            max_length=config.event_max_length,
            block_ms=config.event_block_ms,
        )
        _STREAM_INSTANCES[cache_key] = instance
        return instance

    def publish_event(
        self, *, execution_id: str, event_type: str, payload: dict[str, Any], timestamp: datetime
    ) -> int:
        try:
            result = self.redis.eval(
                _PUBLISH_EVENT_LUA,
                2,
                execution_stream_key(execution_id),
                execution_sequence_key(execution_id),
                self.max_length,
                execution_id,
                event_type,
                timestamp.isoformat(),
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                self.ttl_seconds,
            )
            return int(result)
        except (RedisError, ValueError, TypeError) as exc:
            logger.warning("Redis execution event publication failed: %s", exc)
            raise EventStreamUnavailable(str(exc)) from exc

    def publish(self, events: Sequence[StreamEvent]) -> None:
        """Publish explicit test projections; production uses publish_event."""
        if not events:
            return
        try:
            pipe = self.redis.pipeline(transaction=False)
            for event in events:
                key = execution_stream_key(event.execution_id)
                timestamp = getattr(event, "timestamp", None) or getattr(event, "created_at", None)
                pipe.xadd(
                    key,
                    {
                        "sequence": str(event.sequence),
                        "execution_id": event.execution_id,
                        "type": event.event_type,
                        "timestamp": timestamp.isoformat() if timestamp else "",
                        "data": json.dumps(
                            event.payload, ensure_ascii=False, separators=(",", ":")
                        ),
                    },
                    id=redis_stream_id(event.sequence),
                    maxlen=self.max_length,
                    approximate=True,
                )
                pipe.expire(key, self.ttl_seconds)
                set_value = getattr(pipe, "set", None)
                if callable(set_value):
                    set_value(
                        execution_sequence_key(event.execution_id),
                        event.sequence,
                        ex=self.ttl_seconds,
                    )
            pipe.execute()
        except RedisError as exc:
            raise EventStreamUnavailable(str(exc)) from exc

    def bounds(self, execution_id: str) -> StreamBounds:
        key, sequence_key = execution_stream_key(execution_id), execution_sequence_key(execution_id)
        if not hasattr(self.redis, "xrange"):
            # Minimal test publishers that only implement XREAD predate replay
            # bounds. Production Redis always supports these commands.
            return StreamBounds(None, None, True, True)
        try:
            first = self.redis.xrange(key, min="-", max="+", count=1)
            last = self.redis.xrevrange(key, max="+", min="-", count=1)
            exists = int(self.redis.exists(key, sequence_key))
        except RedisError as exc:
            raise EventStreamUnavailable(str(exc)) from exc
        return StreamBounds(
            _sequence_from_fields(first[0][0], first[0][1]) if first else None,
            _sequence_from_fields(last[0][0], last[0][1]) if last else None,
            bool(exists & 1),
            bool(exists & 2),
        )

    def validate_cursor(
        self, execution_id: str, *, after_sequence: int, first_event_at: datetime | None = None
    ) -> StreamBounds:
        bounds = self.bounds(execution_id)
        cursor = max(0, int(after_sequence))
        if bounds.earliest_sequence is not None and bounds.earliest_sequence > cursor + 1:
            raise StreamReplayGap(
                f"stream begins at {bounds.earliest_sequence}, cursor was {cursor}"
            )
        if bounds.latest_sequence is not None and cursor > bounds.latest_sequence:
            raise InvalidStreamCursor(
                f"cursor {cursor} is after latest sequence {bounds.latest_sequence}"
            )
        if first_event_at is not None and not bounds.stream_exists and not bounds.sequence_exists:
            raise StreamReplayExpired("Redis replay window has expired")
        return bounds

    def read(
        self,
        execution_id: str,
        *,
        after_sequence: int,
        block_ms: int | None = None,
        first_event_at: datetime | None = None,
        validate_cursor: bool = True,
    ) -> list[StreamEvent]:
        if validate_cursor:
            self.validate_cursor(
                execution_id, after_sequence=after_sequence, first_event_at=first_event_at
            )
        try:
            rows = self.redis.xread(
                {execution_stream_key(execution_id): redis_stream_id(after_sequence)},
                block=self.block_ms if block_ms is None else max(1, block_ms),
            )
        except RedisError as exc:
            raise EventStreamUnavailable(str(exc)) from exc
        return _decode_xread(rows)

    def iter_events(self, execution_id: str, *, after_sequence: int) -> Iterator[StreamEvent]:
        cursor = max(0, after_sequence)
        while rows := self.read(execution_id, after_sequence=cursor):
            for event in rows:
                if event.sequence > cursor:
                    cursor = event.sequence
                    yield event


def _decode_xread(response: Any) -> list[StreamEvent]:
    decoded: list[StreamEvent] = []
    for _name, messages in response or []:
        for stream_id, fields in messages:
            try:
                payload = json.loads(fields.get("data", "{}"))
            except (TypeError, json.JSONDecodeError):
                payload = {}
            try:
                timestamp = (
                    datetime.fromisoformat(fields["timestamp"]) if fields.get("timestamp") else None
                )
            except ValueError:
                timestamp = None
            decoded.append(
                StreamEvent(
                    _sequence_from_fields(stream_id, fields),
                    str(fields.get("execution_id") or ""),
                    str(fields.get("type") or "state"),
                    timestamp,
                    payload if isinstance(payload, dict) else {},
                )
            )
    return sorted(decoded, key=lambda item: item.sequence)


def _sequence_from_fields(stream_id: str, fields: dict[str, str]) -> int:
    try:
        return int(fields.get("sequence") or str(stream_id).split("-", 1)[0])
    except (TypeError, ValueError):
        return 0


__all__ = [
    "EventStreamPublisher",
    "EventStreamUnavailable",
    "InvalidStreamCursor",
    "RedisEventStream",
    "StreamBounds",
    "StreamEvent",
    "StreamReplayExpired",
    "StreamReplayGap",
    "execution_sequence_key",
    "execution_stream_key",
    "redis_stream_id",
]
