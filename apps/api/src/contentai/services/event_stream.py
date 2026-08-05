from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import datetime
from math import isfinite
from threading import RLock
from typing import Any, Protocol

from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)
EXECUTION_STREAM_PREFIX = "contentai:execution"
_STREAM_INSTANCES: dict[tuple[str, int, int, int], RedisEventStream] = {}
_PUBLISH_STREAM_INSTANCES: dict[tuple[str, int, int], RedisEventStream] = {}
_STREAM_CACHE_LOCK = RLock()
_PUBLISH_SOCKET_TIMEOUT_SECONDS = 1.0
_INVALID_EVENT_PAYLOAD_MESSAGE = "Invalid Redis execution event payload"
_MAX_EVENT_PAYLOAD_NESTING_DEPTH = 128
MAX_SAFE_EVENT_SEQUENCE = 9_007_199_254_740_991
_TERMINAL_EVENT_TYPES = {
    ("waiting_input", "run_interrupt"): "state",
    ("completed", "run_finish"): "state",
    ("failed", "run_error"): "error",
    ("cancelled", "run_cancel"): "state",
}

# The script validates every piece of Redis history before mutating anything.
# The database-owned committed sequence is passed as an optimistic fence; a
# Redis write that survives a database rollback is therefore never extended by
# a later writer.
_PUBLISH_EVENT_LUA = """
local stream_exists = redis.call('EXISTS', KEYS[1])
local stream_length = redis.call('XLEN', KEYS[1])
local sequence_raw = redis.call('GET', KEYS[2])
local terminal_raw = redis.call('GET', KEYS[3])
local expected_raw = ARGV[7]

if not string.match(expected_raw, '^0$') and not string.match(expected_raw, '^[1-9][0-9]*$') then
  return {'GAP', '0'}
end
local expected_number = tonumber(expected_raw)
if not expected_number or expected_number > 9007199254740991 then
  return {'GAP', '0'}
end
if ARGV[9] == '1' and (ARGV[10] == '' or ARGV[11] == '' or ARGV[12] == '') then
  return {'INVALID', '0'}
end
if ARGV[9] == '1' and ARGV[13] == '1' then
  return {'INVALID', '0'}
end

local current = nil
local latest_data = nil
local latest_execution_id = nil
local latest_event_type = nil
local reopen_terminal = false
if stream_exists == 0 and stream_length == 0 and not sequence_raw then
  if terminal_raw then
    return {'EXPIRED', '0'}
  end
  if ARGV[8] ~= '1' then
    return {'EXPIRED', '0'}
  end
  if expected_raw ~= '0' then
    return {'GAP', '0'}
  end
  current = 0
elseif stream_exists == 1 and stream_length == 0 then
  return {'EXPIRED', '0'}
elseif stream_length == 0 or not sequence_raw then
  return {'EXPIRED', '0'}
else
  if not string.match(sequence_raw, '^[1-9][0-9]*$') then
    return {'GAP', '0'}
  end
  if sequence_raw ~= expected_raw then
    return {'GAP', sequence_raw}
  end
  local latest = redis.call('XREVRANGE', KEYS[1], '+', '-', 'COUNT', 1)
  if #latest ~= 1 or latest[1][1] ~= sequence_raw .. '-0' then
    return {'GAP', sequence_raw}
  end
  local fields = latest[1][2]
  local entry_sequence = nil
  local sequence_fields = 0
  local data_fields = 0
  local execution_id_fields = 0
  local type_fields = 0
  for index = 1, #fields, 2 do
    if fields[index] == 'sequence' then
      entry_sequence = fields[index + 1]
      sequence_fields = sequence_fields + 1
    elseif fields[index] == 'data' then
      latest_data = fields[index + 1]
      data_fields = data_fields + 1
    elseif fields[index] == 'execution_id' then
      latest_execution_id = fields[index + 1]
      execution_id_fields = execution_id_fields + 1
    elseif fields[index] == 'type' then
      latest_event_type = fields[index + 1]
      type_fields = type_fields + 1
    end
  end
  if sequence_fields ~= 1 or data_fields ~= 1
      or execution_id_fields ~= 1 or type_fields ~= 1
      or entry_sequence ~= sequence_raw
      or latest_execution_id ~= ARGV[2] then
    return {'GAP', sequence_raw}
  end
  current = tonumber(sequence_raw)
end

if terminal_raw then
  local terminal_ok, terminal = pcall(cjson.decode, terminal_raw)
  if not terminal_ok or type(terminal) ~= 'table' then
    return {'GAP', sequence_raw or '0'}
  end
  if type(terminal['sequence']) ~= 'string'
      or type(terminal['attempt_id']) ~= 'string'
      or type(terminal['event_name']) ~= 'string'
      or type(terminal['status']) ~= 'string'
      or terminal['attempt_id'] == ''
      or terminal['event_name'] == ''
      or (terminal['status'] ~= 'waiting_input'
          and terminal['status'] ~= 'completed'
          and terminal['status'] ~= 'failed'
          and terminal['status'] ~= 'cancelled') then
    return {'GAP', sequence_raw or '0'}
  end
  local terminal_sequence = terminal['sequence']
  if terminal_sequence ~= expected_raw then
    return {'GAP', terminal_sequence}
  end
  local canonical_terminal_type = nil
  if terminal['status'] == 'waiting_input' and terminal['event_name'] == 'run_interrupt' then
    canonical_terminal_type = 'state'
  elseif terminal['status'] == 'completed' and terminal['event_name'] == 'run_finish' then
    canonical_terminal_type = 'state'
  elseif terminal['status'] == 'failed' and terminal['event_name'] == 'run_error' then
    canonical_terminal_type = 'error'
  elseif terminal['status'] == 'cancelled' and terminal['event_name'] == 'run_cancel' then
    canonical_terminal_type = 'state'
  else
    return {'GAP', terminal_sequence}
  end
  if latest_event_type ~= canonical_terminal_type then
    return {'GAP', terminal_sequence}
  end
  local latest_ok, latest_payload = pcall(cjson.decode, latest_data or '')
  if not latest_ok or type(latest_payload) ~= 'table'
      or type(latest_payload['attempt_id']) ~= 'string'
      or type(latest_payload['name']) ~= 'string'
      or type(latest_payload['terminal_status']) ~= 'string'
      or latest_payload['attempt_id'] ~= terminal['attempt_id']
      or latest_payload['name'] ~= terminal['event_name']
      or latest_payload['terminal_status'] ~= terminal['status'] then
    return {'GAP', terminal_sequence}
  end
  if ARGV[9] == '1'
      and terminal['attempt_id'] == ARGV[10]
      and terminal['event_name'] == ARGV[11]
      and terminal['status'] == ARGV[12] then
    return {'ALREADY_CLOSED', terminal_sequence}
  end
  if ARGV[13] == '1'
      and terminal['attempt_id'] == ARGV[14]
      and terminal['event_name'] == 'run_interrupt'
      and terminal['status'] == ARGV[15]
      and ARGV[15] == 'waiting_input'
      and ARGV[10] ~= ARGV[14] then
    reopen_terminal = true
  else
    return {'CLOSED', terminal_sequence}
  end
end

if ARGV[9] == '1' then
  local payload_ok, terminal_payload = pcall(cjson.decode, ARGV[5])
  if not payload_ok or type(terminal_payload) ~= 'table'
      or type(terminal_payload['attempt_id']) ~= 'string'
      or type(terminal_payload['name']) ~= 'string'
      or type(terminal_payload['terminal_status']) ~= 'string'
      or terminal_payload['attempt_id'] ~= ARGV[10]
      or terminal_payload['name'] ~= ARGV[11]
      or terminal_payload['terminal_status'] ~= ARGV[12] then
    return {'INVALID', expected_raw}
  end
end

if current >= 9007199254740991 then
  return {'GAP', expected_raw}
end
local next_sequence = current + 1
local next_sequence_raw = string.format('%.0f', next_sequence)
local stream_id = next_sequence_raw .. '-0'
redis.call('XADD', KEYS[1], 'MAXLEN', '~', ARGV[1], stream_id,
  'sequence', next_sequence_raw, 'execution_id', ARGV[2],
  'type', ARGV[3], 'timestamp', ARGV[4], 'data', ARGV[5])
redis.call('SET', KEYS[2], next_sequence_raw, 'EX', ARGV[6])
redis.call('EXPIRE', KEYS[1], ARGV[6])
if ARGV[9] == '1' then
  local terminal = cjson.encode({
    sequence = next_sequence_raw,
    attempt_id = ARGV[10],
    event_name = ARGV[11],
    status = ARGV[12]
  })
  redis.call('SET', KEYS[3], terminal, 'EX', ARGV[6])
elseif reopen_terminal then
  redis.call('DEL', KEYS[3])
end
return {'OK', next_sequence_raw}
"""


class EventStreamUnavailable(RuntimeError):
    """Redis, the only live-event transport, could not accept an event."""


class EventStreamPayloadInvalid(RuntimeError):
    """An event could not be represented by the bounded JSON contract."""


class EventStreamPublishExpired(EventStreamUnavailable):
    """Redis history was missing or one-sided while durable history existed."""


class EventStreamPublishGap(EventStreamUnavailable):
    """Redis stream, entry, counter, and database watermarks disagreed."""


class EventStreamClosed(EventStreamUnavailable):
    """The execution stream already has an immutable terminal watermark."""


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
    bounds_known: bool = True
    sequence_value: int | None = None
    terminal_sequence: int | None = None
    terminal_attempt_id: str | None = None
    terminal_event_name: str | None = None
    terminal_status: str | None = None


def execution_stream_key(execution_id: str) -> str:
    return f"{EXECUTION_STREAM_PREFIX}:{execution_id}:events"


def execution_sequence_key(execution_id: str) -> str:
    return f"{EXECUTION_STREAM_PREFIX}:{execution_id}:sequence"


def execution_terminal_sequence_key(execution_id: str) -> str:
    return f"{EXECUTION_STREAM_PREFIX}:{execution_id}:terminal-sequence"


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
        with _STREAM_CACHE_LOCK:
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

    @classmethod
    def publisher_from_settings(cls, settings: Any) -> RedisEventStream:
        """Return a cached publisher whose timeout is independent of blocking XREAD."""
        config = settings.redis
        cache_key = (
            config.url,
            config.event_ttl_seconds,
            config.event_max_length,
        )
        with _STREAM_CACHE_LOCK:
            if cached := _PUBLISH_STREAM_INSTANCES.get(cache_key):
                return cached
            instance = cls(
                Redis.from_url(
                    config.url,
                    decode_responses=True,
                    socket_connect_timeout=0.5,
                    socket_timeout=_PUBLISH_SOCKET_TIMEOUT_SECONDS,
                ),
                ttl_seconds=config.event_ttl_seconds,
                max_length=config.event_max_length,
                block_ms=config.event_block_ms,
            )
            _PUBLISH_STREAM_INSTANCES[cache_key] = instance
            return instance

    def publish_event(
        self,
        *,
        execution_id: str,
        event_type: str,
        payload: dict[str, Any],
        timestamp: datetime,
        expected_sequence: int = 0,
        allow_initialize: bool = True,
        close_stream: bool = False,
        terminal_attempt_id: str | None = None,
        terminal_event_name: str | None = None,
        terminal_status: str | None = None,
        allow_reopen: bool = False,
        reopen_attempt_id: str | None = None,
        reopen_status: str | None = None,
    ) -> int:
        try:
            _validate_finite_json_numbers(payload)
            encoded_payload = json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                allow_nan=False,
            )
        except (RecursionError, TypeError, ValueError) as exc:
            raise EventStreamPayloadInvalid(_INVALID_EVENT_PAYLOAD_MESSAGE) from exc
        expected = max(0, int(expected_sequence))
        if expected > MAX_SAFE_EVENT_SEQUENCE:
            raise EventStreamPublishGap("Database event sequence exceeds the safe range")
        attempt_id = str(terminal_attempt_id or "").strip()
        event_name = str(terminal_event_name or "").strip()
        status = str(terminal_status or "").strip()
        prior_attempt_id = str(reopen_attempt_id or "").strip()
        prior_status = str(reopen_status or "").strip()
        if close_stream and (not attempt_id or not event_name or not status):
            raise EventStreamPayloadInvalid("Terminal stream identity is required")
        try:
            result = self.redis.eval(
                _PUBLISH_EVENT_LUA,
                3,
                execution_stream_key(execution_id),
                execution_sequence_key(execution_id),
                execution_terminal_sequence_key(execution_id),
                self.max_length,
                execution_id,
                event_type,
                timestamp.isoformat(),
                encoded_payload,
                self.ttl_seconds,
                expected,
                "1" if allow_initialize else "0",
                "1" if close_stream else "0",
                attempt_id,
                event_name,
                status,
                "1" if allow_reopen else "0",
                prior_attempt_id,
                prior_status,
            )
        except RedisError as exc:
            logger.warning("Redis execution event publication failed: %s", exc)
            raise EventStreamUnavailable(str(exc)) from exc
        if not isinstance(result, list | tuple) or len(result) != 2:
            raise EventStreamUnavailable("Invalid Redis publication result")
        result_code = str(result[0])
        result_value = str(result[1])
        if result_code in {"OK", "ALREADY_CLOSED"}:
            try:
                sequence = int(result_value)
            except (TypeError, ValueError) as exc:
                raise EventStreamUnavailable("Invalid Redis publication sequence") from exc
            if sequence <= 0:
                raise EventStreamUnavailable("Invalid Redis publication sequence")
            return sequence
        if result_code == "EXPIRED":
            raise EventStreamPublishExpired("Redis execution history has expired")
        if result_code == "GAP":
            raise EventStreamPublishGap("Redis execution history is inconsistent")
        if result_code == "CLOSED":
            raise EventStreamClosed("Redis execution stream is closed")
        if result_code == "INVALID":
            raise EventStreamPayloadInvalid("Invalid terminal stream identity")
        raise EventStreamUnavailable(f"Unknown Redis publication result: {result_code}")

    def publish(self, events: Sequence[StreamEvent]) -> None:
        """Publish explicit test projections; production uses publish_event."""
        if not events:
            return
        try:
            encoded_events = [
                (
                    event,
                    json.dumps(
                        event.payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                        allow_nan=False,
                    ),
                )
                for event in events
            ]
            pipe = self.redis.pipeline(transaction=False)
            for event, encoded_payload in encoded_events:
                key = execution_stream_key(event.execution_id)
                timestamp = getattr(event, "timestamp", None) or getattr(event, "created_at", None)
                pipe.xadd(
                    key,
                    {
                        "sequence": str(event.sequence),
                        "execution_id": event.execution_id,
                        "type": event.event_type,
                        "timestamp": timestamp.isoformat() if timestamp else "",
                        "data": encoded_payload,
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
        except (RedisError, ValueError, TypeError) as exc:
            raise EventStreamUnavailable(str(exc)) from exc

    def bounds(self, execution_id: str) -> StreamBounds:
        key = execution_stream_key(execution_id)
        sequence_key = execution_sequence_key(execution_id)
        terminal_key = execution_terminal_sequence_key(execution_id)
        if not hasattr(self.redis, "xrange"):
            # Minimal test publishers that only implement XREAD predate replay
            # bounds. Production Redis always supports these commands.
            return StreamBounds(None, None, True, True, bounds_known=False)
        try:
            pipe = self.redis.pipeline(transaction=True)
            pipe.xrange(key, min="-", max="+", count=1)
            pipe.xrevrange(key, max="+", min="-", count=1)
            pipe.exists(key)
            pipe.get(sequence_key)
            pipe.get(terminal_key)
            first, last, stream_exists, sequence_raw, terminal_raw = pipe.execute()
        except RedisError as exc:
            raise EventStreamUnavailable(str(exc)) from exc
        stream_exists = bool(stream_exists)
        sequence_exists = sequence_raw is not None
        terminal_exists = terminal_raw is not None
        if stream_exists != sequence_exists:
            raise StreamReplayExpired("Redis replay evidence is one-sided")
        if terminal_exists and not (stream_exists and sequence_exists):
            raise StreamReplayExpired("Redis terminal replay evidence is one-sided")
        if stream_exists and (not first or not last):
            raise StreamReplayExpired("Redis replay stream is empty but still exists")
        if not stream_exists and (first or last):
            raise StreamReplayExpired("Redis replay evidence is one-sided")
        sequence_value = _positive_sequence(sequence_raw) if sequence_exists else None
        earliest_sequence = _strict_sequence_from_entry(first[0]) if first else None
        latest_sequence = _strict_sequence_from_entry(last[0]) if last else None
        if first and earliest_sequence is None or last and latest_sequence is None:
            raise StreamReplayGap("Redis stream entry has an invalid sequence")
        if latest_sequence is not None and sequence_value != latest_sequence:
            raise StreamReplayGap("Redis stream and sequence counter disagree")
        if sequence_exists and sequence_value is None:
            raise StreamReplayGap("Redis sequence counter is invalid")
        terminal_sequence: int | None = None
        terminal_attempt_id: str | None = None
        terminal_event_name: str | None = None
        terminal_status: str | None = None
        if terminal_exists:
            (
                terminal_sequence,
                terminal_attempt_id,
                terminal_event_name,
                terminal_status,
            ) = _decode_terminal_marker(terminal_raw)
            if terminal_sequence is None:
                raise StreamReplayGap("Redis terminal watermark is invalid")
            if terminal_sequence != latest_sequence or terminal_sequence != sequence_value:
                raise StreamReplayGap("Redis terminal watermark disagrees with stream bounds")
            if not last or not _terminal_entry_matches(
                last[0],
                execution_id=execution_id,
                attempt_id=terminal_attempt_id,
                event_name=terminal_event_name,
                status=terminal_status,
            ):
                raise StreamReplayGap("Redis terminal watermark identity is invalid")
        return StreamBounds(
            earliest_sequence,
            latest_sequence,
            stream_exists,
            bool(sequence_exists),
            sequence_value=sequence_value,
            terminal_sequence=terminal_sequence,
            terminal_attempt_id=terminal_attempt_id,
            terminal_event_name=terminal_event_name,
            terminal_status=terminal_status,
        )

    def validate_cursor(
        self,
        execution_id: str,
        *,
        after_sequence: int,
        first_event_at: datetime | None = None,
        history_observed: bool = False,
        committed_sequence: int | None = None,
        terminal_sequence: int | None = None,
        terminal_attempt_id: str | None = None,
        terminal_event_name: str | None = None,
        terminal_status: str | None = None,
        allow_prior_waiting_terminal: bool = False,
    ) -> StreamBounds:
        bounds = self.bounds(execution_id)
        cursor = max(0, int(after_sequence))
        if bounds.stream_exists != bounds.sequence_exists:
            raise StreamReplayExpired("Redis replay evidence is one-sided")
        if (
            bounds.bounds_known
            and bounds.stream_exists
            and bounds.latest_sequence is None
        ):
            raise StreamReplayExpired("Redis replay stream is empty but still exists")
        if bounds.terminal_sequence is not None and not bounds.stream_exists:
            raise StreamReplayExpired("Redis terminal replay evidence has expired")
        if not bounds.stream_exists and (
            bounds.sequence_exists
            or first_event_at is not None
            or history_observed
            or committed_sequence not in {None, 0}
            or terminal_sequence is not None
        ):
            raise StreamReplayExpired("Redis replay window has expired")
        if committed_sequence is not None:
            committed = max(0, int(committed_sequence))
            if bounds.latest_sequence is not None and bounds.latest_sequence != committed:
                raise StreamReplayGap("Redis and database committed watermarks disagree")
            if bounds.latest_sequence is None and committed > 0:
                raise StreamReplayExpired("Committed Redis replay history has expired")
        if terminal_sequence is not None:
            terminal = int(terminal_sequence)
            if bounds.terminal_sequence is None:
                raise StreamReplayExpired("Redis terminal watermark has expired")
            if bounds.terminal_sequence != terminal:
                raise StreamReplayGap("Redis and database terminal watermarks disagree")
            if terminal_attempt_id is not None and (
                bounds.terminal_attempt_id != terminal_attempt_id
            ):
                raise StreamReplayGap("Redis terminal attempt does not match the database")
            if terminal_event_name is not None and (
                bounds.terminal_event_name != terminal_event_name
            ):
                raise StreamReplayGap("Redis terminal event does not match the database")
            if terminal_status is not None and bounds.terminal_status != terminal_status:
                raise StreamReplayGap("Redis terminal status does not match the database")
        elif bounds.terminal_sequence is not None:
            raise StreamReplayGap("Redis closed before the database terminal watermark")
        if allow_prior_waiting_terminal and bounds.terminal_sequence is not None:
            if (
                bounds.terminal_event_name != "run_interrupt"
                or bounds.terminal_status != "waiting_input"
            ):
                raise StreamReplayGap("Only a prior waiting-input marker may be reopened")
        if bounds.earliest_sequence is not None and bounds.earliest_sequence > cursor + 1:
            raise StreamReplayGap(
                f"stream begins at {bounds.earliest_sequence}, cursor was {cursor}"
            )
        if bounds.latest_sequence is not None and cursor > bounds.latest_sequence:
            raise InvalidStreamCursor(
                f"cursor {cursor} is after latest sequence {bounds.latest_sequence}"
            )
        if bounds.bounds_known and bounds.latest_sequence is None and cursor > 0:
            raise InvalidStreamCursor(
                f"cursor {cursor} is after latest sequence 0"
            )
        return bounds

    def read(
        self,
        execution_id: str,
        *,
        after_sequence: int,
        block_ms: int | None = None,
        first_event_at: datetime | None = None,
        history_observed: bool = False,
        validate_cursor: bool = True,
        committed_sequence: int | None = None,
        terminal_sequence: int | None = None,
        terminal_attempt_id: str | None = None,
        terminal_event_name: str | None = None,
        terminal_status: str | None = None,
        allow_prior_waiting_terminal: bool = False,
    ) -> list[StreamEvent]:
        if validate_cursor:
            self.validate_cursor(
                execution_id,
                after_sequence=after_sequence,
                first_event_at=first_event_at,
                history_observed=history_observed,
                committed_sequence=committed_sequence,
                terminal_sequence=terminal_sequence,
                terminal_attempt_id=terminal_attempt_id,
                terminal_event_name=terminal_event_name,
                terminal_status=terminal_status,
                allow_prior_waiting_terminal=allow_prior_waiting_terminal,
            )
        try:
            rows = self.redis.xread(
                {execution_stream_key(execution_id): redis_stream_id(after_sequence)},
                block=self.block_ms if block_ms is None else max(1, block_ms),
            )
        except RedisError as exc:
            raise EventStreamUnavailable(str(exc)) from exc
        try:
            events = _decode_xread(rows, execution_id=execution_id)
        except (RecursionError, TypeError, ValueError):
            logger.warning(_INVALID_EVENT_PAYLOAD_MESSAGE)
            raise EventStreamUnavailable(_INVALID_EVENT_PAYLOAD_MESSAGE) from None
        expected_sequence = max(0, int(after_sequence)) + 1
        for event in events:
            if event.sequence != expected_sequence:
                raise StreamReplayGap(
                    f"expected sequence {expected_sequence}, found {event.sequence}"
                )
            expected_sequence += 1
        if not events:
            # The stream may expire while XREAD is blocked. Re-check after an
            # empty read so Redis or connection-local evidence of prior events
            # cannot be hidden by a vanished stream.
            self.validate_cursor(
                execution_id,
                after_sequence=after_sequence,
                first_event_at=first_event_at,
                history_observed=history_observed,
                committed_sequence=committed_sequence,
                terminal_sequence=terminal_sequence,
                terminal_attempt_id=terminal_attempt_id,
                terminal_event_name=terminal_event_name,
                terminal_status=terminal_status,
                allow_prior_waiting_terminal=allow_prior_waiting_terminal,
            )
        return events

    def iter_events(self, execution_id: str, *, after_sequence: int) -> Iterator[StreamEvent]:
        cursor = max(0, after_sequence)
        while rows := self.read(execution_id, after_sequence=cursor):
            for event in rows:
                if event.sequence > cursor:
                    cursor = event.sequence
                    yield event


def _decode_xread(response: Any, *, execution_id: str) -> list[StreamEvent]:
    decoded: list[StreamEvent] = []
    for _name, messages in response or []:
        for stream_id, fields in messages:
            if not isinstance(fields, dict):
                raise StreamReplayGap("Redis stream entry fields are invalid")
            raw_sequence = fields.get("sequence")
            if not isinstance(raw_sequence, str):
                raise StreamReplayGap("Redis stream entry sequence is invalid")
            sequence = _positive_sequence(raw_sequence)
            if sequence is None or stream_id != redis_stream_id(sequence):
                raise StreamReplayGap("Redis stream entry ID and sequence disagree")
            entry_execution_id = fields.get("execution_id")
            if not isinstance(entry_execution_id, str) or entry_execution_id != execution_id:
                raise StreamReplayGap("Redis stream entry execution does not match request")
            if "data" not in fields:
                raise TypeError(_INVALID_EVENT_PAYLOAD_MESSAGE)
            payload = json.loads(
                fields["data"],
                parse_constant=_reject_nonstandard_json_constant,
            )
            if not isinstance(payload, dict):
                raise TypeError(_INVALID_EVENT_PAYLOAD_MESSAGE)
            _validate_finite_json_numbers(payload)
            try:
                timestamp = (
                    datetime.fromisoformat(fields["timestamp"]) if fields.get("timestamp") else None
                )
            except ValueError:
                timestamp = None
            decoded.append(
                StreamEvent(
                    sequence,
                    entry_execution_id,
                    str(fields.get("type") or "state"),
                    timestamp,
                    payload,
                )
            )
    return decoded


def _reject_nonstandard_json_constant(_value: str) -> Any:
    raise ValueError(_INVALID_EVENT_PAYLOAD_MESSAGE)


def _validate_finite_json_numbers(value: Any) -> None:
    pending = [(value, 0)]
    while pending:
        item, depth = pending.pop()
        if isinstance(item, float):
            if not isfinite(item):
                raise ValueError(_INVALID_EVENT_PAYLOAD_MESSAGE)
        elif isinstance(item, dict):
            if depth > _MAX_EVENT_PAYLOAD_NESTING_DEPTH:
                raise ValueError(_INVALID_EVENT_PAYLOAD_MESSAGE)
            pending.extend((nested, depth + 1) for nested in item.values())
        elif isinstance(item, list):
            if depth > _MAX_EVENT_PAYLOAD_NESTING_DEPTH:
                raise ValueError(_INVALID_EVENT_PAYLOAD_MESSAGE)
            pending.extend((nested, depth + 1) for nested in item)


def _positive_sequence(value: Any) -> int | None:
    raw = str(value or "")
    if not raw.isascii() or not raw.isdigit() or raw.startswith("0"):
        return None
    try:
        sequence = int(raw)
    except (TypeError, ValueError):
        return None
    return sequence if 0 < sequence <= MAX_SAFE_EVENT_SEQUENCE else None


def _strict_sequence_from_entry(entry: Any) -> int | None:
    if not isinstance(entry, list | tuple) or len(entry) != 2:
        return None
    stream_id, fields = entry
    if not isinstance(fields, dict):
        return None
    sequence = _positive_sequence(fields.get("sequence"))
    if sequence is None or str(stream_id) != redis_stream_id(sequence):
        return None
    return sequence


def _decode_terminal_marker(
    value: Any,
) -> tuple[int | None, str | None, str | None, str | None]:
    try:
        decoded = json.loads(str(value))
    except (RecursionError, TypeError, ValueError):
        return None, None, None, None
    if not isinstance(decoded, dict):
        return None, None, None, None
    sequence = _positive_sequence(decoded.get("sequence"))
    attempt_id = decoded.get("attempt_id")
    event_name = decoded.get("event_name")
    status = decoded.get("status")
    if (
        sequence is None
        or not isinstance(attempt_id, str)
        or not attempt_id
        or attempt_id != attempt_id.strip()
        or not isinstance(event_name, str)
        or not event_name
        or event_name != event_name.strip()
        or not isinstance(status, str)
        or status not in {"waiting_input", "completed", "failed", "cancelled"}
    ):
        return None, None, None, None
    return sequence, attempt_id, event_name, status


def _terminal_entry_matches(
    entry: Any,
    *,
    execution_id: str,
    attempt_id: str,
    event_name: str,
    status: str,
) -> bool:
    if not isinstance(entry, list | tuple) or len(entry) != 2:
        return False
    fields = entry[1]
    expected_type = _TERMINAL_EVENT_TYPES.get((status, event_name))
    if (
        not isinstance(fields, dict)
        or expected_type is None
        or not isinstance(fields.get("execution_id"), str)
        or fields["execution_id"] != execution_id
        or not isinstance(fields.get("type"), str)
        or fields["type"] != expected_type
        or "data" not in fields
    ):
        return False
    try:
        payload = json.loads(
            fields["data"],
            parse_constant=_reject_nonstandard_json_constant,
        )
    except (RecursionError, TypeError, ValueError):
        return False
    return bool(
        isinstance(payload, dict)
        and isinstance(payload.get("attempt_id"), str)
        and payload["attempt_id"] == attempt_id
        and isinstance(payload.get("name"), str)
        and payload["name"] == event_name
        and isinstance(payload.get("terminal_status"), str)
        and payload["terminal_status"] == status
    )


def close_cached_event_streams() -> None:
    """Atomically detach and best-effort close every cached Redis client."""
    with _STREAM_CACHE_LOCK:
        instances = [*_STREAM_INSTANCES.values(), *_PUBLISH_STREAM_INSTANCES.values()]
        _STREAM_INSTANCES.clear()
        _PUBLISH_STREAM_INSTANCES.clear()
    clients = {id(instance.redis): instance.redis for instance in instances}
    for client in clients.values():
        closer = getattr(client, "close", None)
        if not callable(closer):
            continue
        try:
            closer()
        except Exception:  # noqa: BLE001
            logger.exception("Failed to close cached Redis event stream client.")


__all__ = [
    "close_cached_event_streams",
    "EventStreamPublisher",
    "EventStreamClosed",
    "EventStreamPayloadInvalid",
    "EventStreamPublishExpired",
    "EventStreamPublishGap",
    "EventStreamUnavailable",
    "InvalidStreamCursor",
    "MAX_SAFE_EVENT_SEQUENCE",
    "RedisEventStream",
    "StreamBounds",
    "StreamEvent",
    "StreamReplayExpired",
    "StreamReplayGap",
    "execution_sequence_key",
    "execution_stream_key",
    "execution_terminal_sequence_key",
    "redis_stream_id",
]
