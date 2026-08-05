import asyncio
import json
import logging
import math
import threading
from collections.abc import Callable, Iterator
from datetime import datetime
from types import SimpleNamespace
from typing import Any

import pytest
from contentai.api.chat import (
    _event_stream_response,
    _to_stream_event_v3,
    replay_run_events,
)
from contentai.api.chat import router as chat_router
from contentai.api.dependencies import (
    get_conversation_service,
    get_current_user,
    get_request_context,
    get_request_session,
)
from contentai.models.schemas import StreamErrorEventV3, StreamPublicEventV3
from contentai.services.errors import StreamingDegradedError
from contentai.services.event_stream import MAX_SAFE_EVENT_SEQUENCE
from fastapi import FastAPI, HTTPException
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient
from pydantic_core import PydanticSerializationError


def _read_sse_body(response: StreamingResponse) -> str:
    async def collect() -> str:
        chunks: list[str] = []
        async for chunk in response.body_iterator:
            chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)
        return "".join(chunks)

    return asyncio.run(collect())


def _frame_data(body: str) -> dict[str, Any]:
    data_lines = [
        line.removeprefix("data: ")
        for line in body.splitlines()
        if line.startswith("data: ")
    ]
    assert len(data_lines) == 1
    return json.loads(data_lines[0])


def test_projection_failure_emits_terminal_control_error_and_stops_before_next_event(
    caplog: pytest.LogCaptureFixture,
) -> None:
    raw_secret = "projection-payload-must-not-leak"
    stop_requested = threading.Event()
    events = iter(
        [
            (
                "state",
                {
                    "execution_id": "execution-1",
                    "name": "execution_started",
                    "raw": raw_secret,
                },
            ),
            (
                "state",
                {
                    "execution_id": "execution-1",
                    "sequence": 2,
                    "name": "execution_completed",
                },
            ),
        ]
    )
    caplog.set_level(logging.WARNING, logger="contentai.api.chat")

    body = _read_sse_body(
        _event_stream_response(
            events,
            execution_id="execution-1",
            stop_requested=stop_requested,
        )
    )

    payload = _frame_data(body)
    assert body.startswith("event: errors\n")
    assert not any(line.startswith("id:") for line in body.splitlines())
    assert payload["data"]["code"] == "STREAM_EXCEPTION_ERROR"
    assert "execution-1:2" not in body
    assert "execution_completed" not in body
    assert raw_secret not in body
    assert raw_secret not in caplog.text
    assert stop_requested.is_set()


def test_naive_iso_timestamp_falls_back_to_an_aware_wire_timestamp() -> None:
    body = _read_sse_body(
        _event_stream_response(
            iter(
                [
                    (
                        "state",
                        {
                            "execution_id": "execution-1",
                            "sequence": 1,
                            "name": "execution_started",
                            "timestamp": "2026-08-04T08:00:00",
                        },
                    )
                ]
            ),
            execution_id="execution-1",
        )
    )

    payload = _frame_data(body)
    timestamp = datetime.fromisoformat(payload["timestamp"].replace("Z", "+00:00"))
    assert body.startswith("id: execution-1:1\nevent: lifecycle\n")
    assert timestamp.tzinfo is not None
    assert timestamp.utcoffset() is not None
    assert "STREAM_EXCEPTION_ERROR" not in body


@pytest.mark.parametrize("exception_type", [PydanticSerializationError, RuntimeError])
def test_event_serialization_failure_emits_one_private_terminal_control(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    exception_type: type[Exception],
) -> None:
    raw_secret = "serializer-payload-must-not-leak"
    stop_requested = threading.Event()

    def fail_model_dump(_self: Any, *_args: Any, **_kwargs: Any) -> Any:
        raise exception_type(raw_secret)

    monkeypatch.setattr(StreamPublicEventV3, "model_dump", fail_model_dump)
    caplog.set_level(logging.WARNING, logger="contentai.api.chat")
    body = _read_sse_body(
        _event_stream_response(
            iter(
                [
                    (
                        "state",
                        {
                            "execution_id": "execution-1",
                            "sequence": 1,
                            "name": "execution_started",
                            "content": raw_secret,
                        },
                    ),
                    (
                        "state",
                        {
                            "execution_id": "execution-1",
                            "sequence": 2,
                            "name": "execution_completed",
                        },
                    ),
                ]
            ),
            execution_id="execution-1",
            stop_requested=stop_requested,
        )
    )

    payload = _frame_data(body)
    assert body.count("event: errors\n") == 1
    assert not any(line.startswith("id:") for line in body.splitlines())
    assert payload["data"]["code"] == "STREAM_EXCEPTION_ERROR"
    assert "execution-1:2" not in body
    assert "execution_completed" not in body
    assert raw_secret not in body
    assert raw_secret not in caplog.text
    assert stop_requested.is_set()


@pytest.mark.parametrize(
    ("public_field", "non_finite_value"),
    [
        ("progress", math.nan),
        ("content", math.inf),
        ("progress", -math.inf),
    ],
)
def test_non_finite_public_payload_emits_one_standard_json_control(
    caplog: pytest.LogCaptureFixture,
    public_field: str,
    non_finite_value: float,
) -> None:
    raw_secret = "non-finite-payload-must-not-leak"
    stop_requested = threading.Event()
    caplog.set_level(logging.WARNING, logger="contentai.api.chat")

    body = _read_sse_body(
        _event_stream_response(
            iter(
                [
                    (
                        "state",
                        {
                            "execution_id": "execution-1",
                            "sequence": 1,
                            "name": "execution_started",
                            public_field: non_finite_value,
                            "marker": raw_secret,
                        },
                    ),
                    (
                        "state",
                        {
                            "execution_id": "execution-1",
                            "sequence": 2,
                            "name": "execution_completed",
                        },
                    ),
                ]
            ),
            execution_id="execution-1",
            stop_requested=stop_requested,
        )
    )

    payload = _frame_data(body)
    assert body.count("event: errors\n") == 1
    assert not any(line.startswith("id:") for line in body.splitlines())
    assert payload["data"]["code"] == "STREAM_EXCEPTION_ERROR"
    assert all(token not in body for token in ("NaN", "Infinity", "-Infinity"))
    assert "execution-1:2" not in body
    assert "execution_completed" not in body
    assert raw_secret not in body
    assert raw_secret not in caplog.text
    assert stop_requested.is_set()


def test_unknown_stream_exception_uses_fixed_private_control_error_code() -> None:
    class PrivatePythonFailure(RuntimeError):
        pass

    def events() -> Iterator[tuple[str, dict[str, Any]]]:
        raise PrivatePythonFailure("provider payload must stay private")
        yield ("state", {})

    body = _read_sse_body(
        _event_stream_response(
            events(),
            execution_id="execution-1",
        )
    )

    payload = _frame_data(body)
    assert payload["data"] == {
        "name": "stream_exception",
        "code": "STREAM_EXCEPTION_ERROR",
        "message": "Event stream processing failed.",
    }
    assert "PrivatePythonFailure" not in body
    assert "provider payload" not in body
    assert not any(line.startswith("id:") for line in body.splitlines())
    assert "Last-Event-ID" not in body


def test_sequenced_error_projection_has_typed_nonempty_data_and_keeps_safe_extras() -> None:
    stream_event = _to_stream_event_v3(
        "error",
        {
            "execution_id": "execution-1",
            "sequence": 1,
            "name": "run_error",
            "error_code": "PROVIDER_FAILURE",
            "retryable": True,
            "marker": "safe-public-marker",
        },
    )

    assert isinstance(stream_event, StreamErrorEventV3)
    assert stream_event.channel == "errors"
    assert stream_event.data.code == "PROVIDER_FAILURE"
    assert stream_event.data.message == "The execution reported an error."
    assert stream_event.data.name == "run_error"
    assert stream_event.data.retryable is True
    assert stream_event.data.model_dump()["marker"] == "safe-public-marker"


def test_stream_projection_accepts_max_safe_sequence_and_rejects_next_value() -> None:
    event = _to_stream_event_v3(
        "state",
        {
            "execution_id": "execution-1",
            "sequence": MAX_SAFE_EVENT_SEQUENCE,
            "name": "execution_completed",
        },
    )

    assert event.sequence == MAX_SAFE_EVENT_SEQUENCE
    with pytest.raises(ValueError, match="outside the safe range"):
        _to_stream_event_v3(
            "state",
            {
                "execution_id": "execution-1",
                "sequence": MAX_SAFE_EVENT_SEQUENCE + 1,
                "name": "execution_completed",
            },
        )
class _CursorService:
    def __init__(self) -> None:
        self.validated: list[int] = []
        self.replayed: list[int] = []

    @staticmethod
    def get_execution_status(_session: Any, execution_id: str, _auth: Any) -> Any:
        return SimpleNamespace(id=execution_id, session_id="session-1")

    def validate_execution_event_cursor(
        self,
        _session: Any,
        _execution_id: str,
        _auth: Any,
        *,
        after_sequence: int,
    ) -> None:
        self.validated.append(after_sequence)

    def replay_execution_events(
        self,
        _session_bind: Any,
        _execution_id: str,
        _auth: Any,
        *,
        after_sequence: int,
        **_kwargs: Any,
    ) -> Iterator[tuple[str, dict[str, Any]]]:
        self.replayed.append(after_sequence)
        return iter(())


def _build_stream_test_app(
    service: Any,
    session_dependency: Callable[[], Iterator[Any]],
) -> FastAPI:
    app = FastAPI()
    app.include_router(chat_router, prefix="/api")
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(
        user_id="local-user"
    )
    app.dependency_overrides[get_request_context] = lambda: SimpleNamespace(
        request_id="request-1"
    )
    app.dependency_overrides[get_conversation_service] = lambda: service
    app.dependency_overrides[get_request_session] = session_dependency
    return app


def _simple_session_dependency() -> Iterator[Any]:
    yield SimpleNamespace(get_bind=lambda: object())


def test_decorated_replay_preflight_degradation_returns_409_without_creating_replay() -> None:
    class PreflightDegradedService(_CursorService):
        def __init__(self) -> None:
            super().__init__()
            self.replay_created = False

        def validate_execution_event_cursor(
            self,
            _session: Any,
            _execution_id: str,
            _auth: Any,
            *,
            after_sequence: int,
        ) -> None:
            self.validated.append(after_sequence)
            raise StreamingDegradedError("private redis failure detail")

        def replay_execution_events(self, *_args: Any, **_kwargs: Any) -> Any:
            self.replay_created = True
            raise AssertionError("replay must not be created after failed preflight")

    service = PreflightDegradedService()
    session = SimpleNamespace(get_bind=lambda: object())

    with pytest.raises(HTTPException) as caught:
        replay_run_events(
            "execution-1",
            session,
            None,
            SimpleNamespace(request_id="request-1"),
            service,
            last_event_id=None,
            after_sequence="3",
        )

    assert caught.value.status_code == 409
    assert caught.value.detail["code"] == "STREAMING_DEGRADED"
    assert service.validated == [3]
    assert service.replay_created is False


def test_http_stream_releases_preflight_session_before_first_body_chunk() -> None:
    bind = object()
    preflight_released = threading.Event()

    class TrackedPreflightSession:
        @staticmethod
        def get_bind() -> object:
            return bind

    preflight_session = TrackedPreflightSession()

    def tracked_session_dependency() -> Iterator[Any]:
        try:
            yield preflight_session
        finally:
            preflight_released.set()

    class ReleaseAwareService(_CursorService):
        def __init__(self) -> None:
            super().__init__()
            self.replay_bind: Any = None
            self.replay_started_after_release = False

        def replay_execution_events(
            self,
            session_bind: Any,
            _execution_id: str,
            _auth: Any,
            *,
            after_sequence: int,
            **_kwargs: Any,
        ) -> Iterator[tuple[str, dict[str, Any]]]:
            self.replayed.append(after_sequence)
            self.replay_bind = session_bind

            def events() -> Iterator[tuple[str, dict[str, Any]]]:
                assert preflight_released.is_set()
                self.replay_started_after_release = True
                yield (
                    "state",
                    {
                        "execution_id": "execution-1",
                        "sequence": 1,
                        "name": "execution_started",
                    },
                )

            return events()

    service = ReleaseAwareService()
    app = _build_stream_test_app(service, tracked_session_dependency)

    with TestClient(app) as client:
        with client.stream(
            "GET",
            "/api/chat/runs/execution-1/events",
            headers={"Last-Event-ID": "0"},
        ) as response:
            assert response.status_code == 200
            assert preflight_released.is_set()
            assert service.replay_bind is bind
            first_chunk = next(response.iter_text())

    assert service.replay_started_after_release is True
    assert "id: execution-1:1" in first_chunk


@pytest.mark.parametrize("after_sequence", ["not-an-integer", "-1"])
def test_http_valid_header_ignores_malformed_query(after_sequence: str) -> None:
    service = _CursorService()
    app = _build_stream_test_app(service, _simple_session_dependency)

    with TestClient(app) as client:
        response = client.get(
            f"/api/chat/runs/execution-1/events?after_sequence={after_sequence}",
            headers={"Last-Event-ID": "execution-1:5"},
        )

    assert response.status_code == 200
    assert service.validated == [5]
    assert service.replayed == [5]


@pytest.mark.parametrize(
    ("cursor_source", "cursor", "expected_status"),
    [
        ("query", MAX_SAFE_EVENT_SEQUENCE, 200),
        ("query", MAX_SAFE_EVENT_SEQUENCE + 1, 422),
        ("header", MAX_SAFE_EVENT_SEQUENCE, 200),
        ("header", MAX_SAFE_EVENT_SEQUENCE + 1, 422),
    ],
)
def test_http_event_cursor_enforces_max_safe_sequence(
    cursor_source: str,
    cursor: int,
    expected_status: int,
) -> None:
    service = _CursorService()
    app = _build_stream_test_app(service, _simple_session_dependency)
    query = f"?after_sequence={cursor}" if cursor_source == "query" else ""
    headers = (
        {"Last-Event-ID": f"execution-1:{cursor}"}
        if cursor_source == "header"
        else {}
    )

    with TestClient(app) as client:
        response = client.get(
            f"/api/chat/runs/execution-1/events{query}",
            headers=headers,
        )

    assert response.status_code == expected_status
    if expected_status == 200:
        assert service.validated == [cursor]
        assert service.replayed == [cursor]
    else:
        assert service.validated == []
        assert service.replayed == []


@pytest.mark.parametrize("after_sequence", ["not-an-integer", "-1"])
def test_http_invalid_query_without_header_returns_string_detail_422(
    after_sequence: str,
) -> None:
    service = _CursorService()
    app = _build_stream_test_app(service, _simple_session_dependency)

    with TestClient(app) as client:
        response = client.get(
            f"/api/chat/runs/execution-1/events?after_sequence={after_sequence}"
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "after_sequence must be a non-negative integer"
    }
    assert service.validated == []
    assert service.replayed == []


def test_http_mismatched_header_wins_over_valid_query() -> None:
    service = _CursorService()
    app = _build_stream_test_app(service, _simple_session_dependency)

    with TestClient(app) as client:
        response = client.get(
            "/api/chat/runs/execution-1/events?after_sequence=7",
            headers={"Last-Event-ID": "different-execution:5"},
        )

    assert response.status_code == 422
    assert response.json() == {
        "detail": "Last-Event-ID execution does not match request"
    }
    assert service.validated == []
    assert service.replayed == []


@pytest.mark.parametrize(
    ("last_event_id", "after_sequence", "expected_cursor"),
    [
        (None, None, 0),
        (None, "7", 7),
        ("", "7", 7),
        ("   ", "7", 7),
        ("9", None, 9),
        ("execution-1:8", None, 8),
        ("execution-1:8", "7", 8),
        ("execution-1:8", "not-an-integer", 8),
        ("execution-1:8", "-1", 8),
    ],
)
def test_event_cursor_uses_nonempty_header_before_query(
    last_event_id: str | None,
    after_sequence: str | None,
    expected_cursor: int,
) -> None:
    service = _CursorService()
    session = SimpleNamespace(get_bind=lambda: object())

    response = replay_run_events(
        "execution-1",
        session,
        None,
        SimpleNamespace(request_id="request-1"),
        service,
        last_event_id=last_event_id,
        after_sequence=after_sequence,
    )

    assert isinstance(response, StreamingResponse)
    assert service.validated == [expected_cursor]
    assert service.replayed == [expected_cursor]


@pytest.mark.parametrize(
    ("last_event_id", "expected_detail"),
    [
        ("different-execution:8", "Last-Event-ID execution does not match request"),
        ("not-a-sequence", "Last-Event-ID must be a non-negative integer"),
    ],
)
def test_invalid_nonempty_header_does_not_fall_back_to_valid_query(
    last_event_id: str,
    expected_detail: str,
) -> None:
    service = _CursorService()
    session = SimpleNamespace(get_bind=lambda: object())

    with pytest.raises(HTTPException) as caught:
        replay_run_events(
            "execution-1",
            session,
            None,
            SimpleNamespace(request_id="request-1"),
            service,
            last_event_id=last_event_id,
            after_sequence="7",
        )

    assert caught.value.status_code == 422
    assert caught.value.detail == expected_detail
    assert service.validated == []
    assert service.replayed == []


@pytest.mark.parametrize("after_sequence", ["", "not-an-integer", "-1"])
def test_invalid_query_is_rejected_only_when_header_is_empty(
    after_sequence: str,
) -> None:
    service = _CursorService()
    session = SimpleNamespace(get_bind=lambda: object())

    with pytest.raises(HTTPException) as caught:
        replay_run_events(
            "execution-1",
            session,
            None,
            SimpleNamespace(request_id="request-1"),
            service,
            last_event_id=" ",
            after_sequence=after_sequence,
        )

    assert caught.value.status_code == 422
    assert caught.value.detail == "after_sequence must be a non-negative integer"
    assert service.validated == []
    assert service.replayed == []
