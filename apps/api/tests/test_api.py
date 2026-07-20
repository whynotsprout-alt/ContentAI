import json
import logging
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import pytest
import services.conversation_service as conversation_service_module
from agent.runtime.checkpoint import RuntimePersistence, checkpoint_interrupts
from agent.runtime.container import RuntimeContainer
from api.app import create_app
from api.chat import _stream_channel, _stream_exception_payload
from api.chat import router as chat_router
from auth_helpers import auth_headers, default_test_auth_context, resolve_test_auth_context
from client import ApiClient as TestClient
from core.config import Settings, get_settings
from core.security import authenticate_request
from db.session import get_engine
from direct_dispatcher import DirectDispatcher
from langchain_core.messages import AIMessage
from memory.message_persister import MessagePersister
from models.agent import AgentProfile, AgentVersion
from models.base import utcnow
from models.chat import (
    AgentExecution,
    AgentExecutionAttempt,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    ExecutionOutbox,
    ExecutionResumeRequest,
    ToolExecution,
)
from models.enums import ExecutionAttemptStatus, MessageRole, MessageType, RunStatus
from models.memory import MemoryRecord
from models.user import AdminAuditLog, AppUser
from services.agent_service import AgentService
from services.errors import StreamingDegradedError, StreamReplayExpiredError, StreamReplayGapError
from services.execution_claim import claim_execution
from sqlalchemy import event, text
from sqlmodel import Session, select

_TEST_RUNTIME: RuntimeContainer | None = None
app = create_app(execution_dispatcher_factory=DirectDispatcher)
app.dependency_overrides[authenticate_request] = default_test_auth_context


def account_payload(
    *,
    name: str,
    description: str,
    hotspot_sources: list[str] | None = None,
    topic_prompt: str = "Score topics from 0 to 100.",
    creation_prompt: str = "Write a concise title and body.",
) -> dict[str, Any]:
    return {
        "name": name,
        "description": description,
        "topic_scoring_prompt": topic_prompt,
        "content_prompt": creation_prompt,
        "hotspot_sources": ["douyin", "weibo"] if hotspot_sources is None else hotspot_sources,
    }


def auth_test_app():
    test_app = create_app(
        Settings(
            env="test",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
            },
        ),
        runtime=_TEST_RUNTIME,
        execution_dispatcher_factory=DirectDispatcher,
    )
    test_app.dependency_overrides[authenticate_request] = resolve_test_auth_context
    return test_app


class FakeModel:
    def __init__(self, responses: Iterable[AIMessage]) -> None:
        self.responses = iter(responses)

        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(messages)

        return next(self.responses)


class FakeStructuredModel:
    def __init__(
        self,
        schema: type[Any],
        *,
        title: str = "Generated Title",
        memories: list[dict[str, Any]] | None = None,
        error: Exception | None = None,
    ) -> None:
        self.schema = schema

        self.title = title

        self.memories = memories or []

        self.error = error

    def invoke(self, prompt: str) -> Any:
        if self.error is not None:
            raise self.error

        if self.schema.__name__ == "MemoryExtractionResult":
            return self.schema(memories=self.memories)

        return self.schema(title=self.title)


class FakeHotspotFilterModel:
    def invoke(self, messages: list[Any]) -> AIMessage:
        return AIMessage(content="Filtered hotspots")


class FakeGateway:
    def __init__(self, *, title: str = "Generated Title", error: Exception | None = None) -> None:
        self.title = title

        self.error = error

    def build_agent_model(self, *, tools: list[Any] | None = None) -> FakeModel:
        if getattr(self, "model", None) is None:
            raise AssertionError("agent model was not configured")

        return self.model

    def build_structured_output_model(self, schema: type[Any]) -> FakeStructuredModel:
        return FakeStructuredModel(
            schema,
            title=self.title,
            memories=getattr(self, "memories", []),
            error=self.error,
        )

    def build_hotspot_filter_model(self) -> FakeHotspotFilterModel:
        return FakeHotspotFilterModel()


def install_fake_model(
    model: FakeModel,
    *,
    memories: list[dict[str, Any]] | None = None,
    title: str = "Generated Title",
    error: Exception | None = None,
) -> None:
    global _TEST_RUNTIME

    if _TEST_RUNTIME is not None:
        _TEST_RUNTIME.close()

    gateway = FakeGateway(title=title, error=error)

    gateway.model = model

    gateway.memories = memories or []

    _TEST_RUNTIME = RuntimeContainer(settings=get_settings(), model_gateway=gateway)

    app.state.runtime = _TEST_RUNTIME


def wait_for_terminal_session(client: TestClient, session_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10

    payload: dict[str, Any] = {}

    while time.monotonic() < deadline:
        response = client.get(f"/api/chat/sessions/{session_id}")

        assert response.status_code == 200

        payload = response.json()

        status = (payload.get("latest_execution") or {}).get("status")

        if status in {"completed", "failed", "cancelled", "waiting_input"}:
            return payload

        time.sleep(0.1)

    raise AssertionError(f"run did not finish in time: {payload}")


def wait_for_session_title(client: TestClient, *, expected_title: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5

    sessions: list[dict[str, Any]] = []

    while time.monotonic() < deadline:
        response = client.get("/api/chat/sessions")

        assert response.status_code == 200

        sessions = response.json()["items"]

        if sessions and sessions[0]["title"] == expected_title:
            return sessions

        time.sleep(0.1)

    raise AssertionError(f"session title did not update in time: {sessions}")


def test_account_crud_contract():
    payload = account_payload(
        name="Test Account",
        description="Account for API contract tests.",
        hotspot_sources=["douyin", "weibo"],
    )

    with TestClient(app) as client:
        created = client.post("/api/agents", json=payload)
        assert created.status_code == 201
        account_id = created.json()["id"]
        assert created.json()["description"] == payload["description"]
        assert (
            created.json()["current_version"]["topic_scoring_prompt"]
            == payload["topic_scoring_prompt"]
        )
        assert created.json()["current_version"]["hotspot_sources"] == [
            "douyin",
            "weibo",
        ]

        updated = client.patch(
            f"/api/agents/{account_id}",
            json={"name": "Test Account Updated", "description": "Updated account description."},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "Test Account Updated"
        assert updated.json()["description"] == "Updated account description."

        version = client.post(
            f"/api/agents/{account_id}/versions",
            json={
                "topic_scoring_prompt": "Prioritize relevance and evidence quality.",
                "content_prompt": payload["content_prompt"],
                "hotspot_sources": ["xiaohongshu"],
            },
        )
        assert version.status_code == 201
        assert (
            version.json()["topic_scoring_prompt"] == "Prioritize relevance and evidence quality."
        )
        assert version.json()["hotspot_sources"] == ["xiaohongshu"]

        deleted = client.delete(f"/api/agents/{account_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/agents/{account_id}").status_code == 404


def test_account_names_are_unique_per_user():
    payload = account_payload(
        name="Shared Account",
        description="Account for user uniqueness tests.",
    )

    with Session(get_engine()) as session:
        session.add(
            AppUser(
                id="other-user",
                email="other@test.invalid",
                email_normalized="other@test.invalid",
                password_hash="test-only-password-hash",
                status="active",
                email_verified_at=datetime.now(UTC),
            )
        )
        session.commit()

    with TestClient(auth_test_app()) as client:
        created = client.post(
            "/api/agents",
            headers=auth_headers(user_id="local-user"),
            json=payload,
        )
        duplicate = client.post(
            "/api/agents",
            headers=auth_headers(user_id="local-user"),
            json=payload,
        )
        other_user = client.post(
            "/api/agents",
            headers=auth_headers(user_id="other-user"),
            json=payload,
        )

    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert other_user.status_code == 201

    with Session(get_engine()) as session:
        rows = session.exec(
            select(AgentProfile.user_id).where(AgentProfile.name == payload["name"])
        ).all()
    assert sorted(rows) == ["local-user", "other-user"]


def test_accounts_are_isolated_by_user():
    payload = account_payload(
        name="User Account",
        description="User-scoped account.",
        hotspot_sources=["weibo"],
    )

    with Session(get_engine()) as session:
        session.add(
            AppUser(
                id="other-user",
                email="other@test.invalid",
                email_normalized="other@test.invalid",
                password_hash="test-only-password-hash",
                status="active",
                email_verified_at=datetime.now(UTC),
            )
        )
        session.commit()

    with TestClient(auth_test_app()) as client:
        created = client.post(
            "/api/agents",
            headers=auth_headers(user_id="local-user"),
            json=payload,
        )
        assert created.status_code == 201
        account_id = created.json()["id"]

        owner_list = client.get("/api/agents", headers=auth_headers(user_id="local-user"))
        other_list = client.get("/api/agents", headers=auth_headers(user_id="other-user"))
        other_get = client.get(
            f"/api/agents/{account_id}",
            headers=auth_headers(user_id="other-user"),
        )
        other_update = client.patch(
            f"/api/agents/{account_id}",
            headers=auth_headers(user_id="other-user"),
            json={"name": "Blocked Rename"},
        )
        other_delete = client.delete(
            f"/api/agents/{account_id}",
            headers=auth_headers(user_id="other-user"),
        )

    assert account_id in [account["id"] for account in owner_list.json()]
    assert other_list.json() == []
    assert other_get.status_code == 404
    assert other_update.status_code == 404
    assert other_delete.status_code == 404


def test_account_put_not_allowed():
    payload = account_payload(
        name="Removed Put Test",
        description="Account for method compatibility tests.",
    )

    with TestClient(app) as client:
        created = client.post("/api/agents", json=payload)
        assert created.status_code == 201
        account_id = created.json()["id"]
        not_allowed = client.put(
            f"/api/agents/{account_id}",
            json={"name": "Should not work"},
        )

    assert not_allowed.status_code == 405


def test_patch_account_partial_updates_are_supported():
    payload = account_payload(
        name="Original Account",
        description="Original positioning.",
        hotspot_sources=["douyin", "weibo"],
    )

    with TestClient(app) as client:
        created = client.post("/api/agents", json=payload)
        assert created.status_code == 201
        account_id = created.json()["id"]
        partial = client.patch(
            f"/api/agents/{account_id}",
            json={"name": "Original Account Updated"},
        )
        after_partial = client.get(f"/api/agents/{account_id}")

    assert partial.status_code == 200
    assert partial.json()["name"] == "Original Account Updated"
    assert partial.json()["description"] == payload["description"]
    assert partial.json()["current_version"]["hotspot_sources"] == [
        "douyin",
        "weibo",
    ]
    assert after_partial.status_code == 200
    assert after_partial.json()["name"] == "Original Account Updated"


def test_patch_account_multiple_fields_update():
    payload = account_payload(
        name="Multi Update Account",
        description="Original positioning.",
        hotspot_sources=["weibo"],
    )

    with TestClient(app) as client:
        created = client.post("/api/agents", json=payload)
        assert created.status_code == 201
        account_id = created.json()["id"]
        multi = client.patch(
            f"/api/agents/{account_id}",
            json={"description": "Updated positioning."},
        )
        version = client.post(
            f"/api/agents/{account_id}/versions",
            json={
                "topic_scoring_prompt": payload["topic_scoring_prompt"],
                "content_prompt": payload["content_prompt"],
                "hotspot_sources": ["xiaohongshu", "weibo"],
            },
        )

    assert multi.status_code == 200
    assert multi.json()["description"] == "Updated positioning."
    assert multi.json()["name"] == payload["name"]
    assert version.status_code == 201
    assert version.json()["hotspot_sources"] == ["xiaohongshu", "weibo"]


def test_patch_nonexistent_account_returns_404():
    with TestClient(app) as client:
        missing = client.patch(
            "/api/agents/does-not-exist",
            json={"name": "Ghost Account"},
        )

    assert missing.status_code == 404


def test_account_updated_at_is_touched_by_orm_update_event():
    old_updated_at = datetime(2020, 1, 1, tzinfo=UTC)

    with Session(get_engine()) as session:
        session.execute(
            text("UPDATE agentprofile SET updated_at = :updated_at WHERE id = 'default-agent'"),
            {"updated_at": old_updated_at},
        )
        session.commit()

        account = session.get(AgentProfile, "default-agent")
        assert account is not None
        assert account.updated_at == old_updated_at

        account.description = "Updated without calling touch_updated_at explicitly."
        session.add(account)
        session.commit()
        session.refresh(account)
        updated_at = account.updated_at

    assert updated_at > old_updated_at


def test_account_hotspot_sources_are_stored_as_jsonb():
    with Session(get_engine()) as session:
        source_type = session.execute(
            text(
                "SELECT pg_typeof(hotspot_sources)::text "
                "FROM agentversion WHERE id = 'default-agent-v1'"
            )
        ).scalar_one()

    assert source_type == "jsonb"


def test_account_delete_rejects_referenced_account():
    with Session(get_engine()) as session:
        chat = ChatSession(
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )

        session.add(chat)

        session.commit()

    with TestClient(app) as client:
        deleted = client.delete("/api/agents/default-agent")

        assert deleted.status_code == 409


def test_account_rejects_empty_or_unknown_hotspot_sources():
    payload = account_payload(
        name="Invalid Account",
        description="Account for validation tests.",
        hotspot_sources=[],
        topic_prompt="Test scoring prompt",
        creation_prompt="Test creation prompt",
    )

    with TestClient(app) as client:
        empty_sources = client.post("/api/agents", json=payload)
        assert empty_sources.status_code == 422

        unknown_sources = client.post(
            "/api/agents",
            json={
                **payload,
                "hotspot_sources": ["unknown"],
            },
        )
        assert unknown_sources.status_code == 422


def test_test_identity_override_requires_explicit_headers():
    with TestClient(auth_test_app()) as client:
        response = client.get("/api/agents")

    assert response.status_code == 401


def test_auth_filters_agents_by_allowed_agent_ids():
    with Session(get_engine()) as session:
        session.add(
            AgentProfile(
                id="other-agent",
                user_id="local-user",
                name="Other Account",
                description="Other account",
            )
        )
        session.add(
            AgentVersion(
                id="other-agent-v1",
                agent_id="other-agent",
                version=1,
                content_prompt="Test",
                hotspot_sources=["weibo"],
            )
        )

        session.commit()

    with TestClient(auth_test_app()) as client:
        listed = client.get(
            "/api/agents",
            headers=auth_headers(
                user_id="local-user",
                agents=["default-agent"],
            ),
        )

        forbidden = client.get(
            "/api/agents/other-agent",
            headers=auth_headers(
                user_id="local-user",
                agents=["default-agent"],
            ),
        )

    assert listed.status_code == 200

    assert [account["id"] for account in listed.json()] == ["default-agent"]

    assert forbidden.status_code == 403


def test_auth_blocks_cross_user_chat_access():
    with TestClient(auth_test_app()) as client:
        created = client.post(
            "/api/chat/sessions",
            headers=auth_headers(user_id="local-user"),
            json={"agent_id": "default-agent"},
        )

        assert created.status_code == 200

        session_id = created.json()["session_id"]

        blocked = client.get(
            f"/api/chat/sessions/{session_id}",
            headers=auth_headers(user_id="other-user"),
        )

    assert blocked.status_code == 404


def test_run_routes_are_public_api():
    with TestClient(app) as client:
        created = client.post("/api/chat/runs", json={})

        fetched = client.get("/api/chat/runs/run_missing")

        cancelled = client.post("/api/chat/runs/run_missing/cancel")

    assert created.status_code == 404

    assert fetched.status_code == 404

    assert cancelled.status_code == 404


def test_removed_post_sse_routes_stay_unavailable():
    route_paths = {getattr(route, "path", "") for route in chat_router.routes}

    assert "/chat/sessions/{session_id}/messages/stream" not in route_paths
    assert "/chat/runs/{execution_id}/resume/stream" not in route_paths
    assert "/chat/runs/{execution_id}/events" in route_paths
    assert "/chat/runs/{execution_id}/status" in route_paths


def test_lightweight_run_status_exposes_queue_stages():
    with TestClient(app) as client:
        chat = client.post(
            "/api/chat/sessions",
            json={"agent_id": "default-agent"},
        ).json()
        with Session(get_engine()) as session:
            invocation = AgentInvocation(
                session_id=chat["session_id"],
                agent_id="default-agent",
                user_id="local-user",
            )
            session.add(invocation)
            session.flush()
            execution = AgentExecution(
                invocation_id=invocation.id,
                session_id=chat["session_id"],
                agent_version_id="default-agent-v1",
                status=RunStatus.pending,
            )
            session.add(execution)
            session.flush()
            outbox = ExecutionOutbox(execution_id=execution.id, kind="execute")
            session.add(outbox)
            session.commit()
            execution_id = execution.id
            outbox_id = outbox.id

        response = client.get(f"/api/chat/runs/{execution_id}/status")
        assert response.status_code == 200
        assert response.json()["queue_stage"] == "dispatching"
        assert "messages" not in response.json()
        assert "memory" not in response.json()

        with Session(get_engine()) as session:
            outbox = session.get(ExecutionOutbox, outbox_id)
            assert outbox is not None
            outbox.status = "published"
            session.add(outbox)
            session.commit()
        assert client.get(f"/api/chat/runs/{execution_id}/status").json()[
            "queue_stage"
        ] == "waiting_worker"

        with Session(get_engine()) as session:
            execution = session.get(AgentExecution, execution_id)
            assert execution is not None
            execution.claimed_at = datetime.now(UTC)
            session.add(execution)
            session.commit()
        assert client.get(f"/api/chat/runs/{execution_id}/status").json()[
            "queue_stage"
        ] == "starting"


def test_message_contract_rejects_agent_id_and_creates_no_execution():
    with TestClient(app) as client:
        chat = client.post(
            "/api/chat/sessions",
            json={"agent_id": "default-agent"},
        ).json()
        other_agent = client.post(
            "/api/agents",
            json=account_payload(name="Other agent", description="Mismatch target"),
        )
        assert other_agent.status_code == 201

        with Session(get_engine()) as session:
            before = (
                len(session.exec(select(ChatMessage)).all()),
                len(session.exec(select(AgentInvocation)).all()),
                len(session.exec(select(AgentExecution)).all()),
            )

        response = client.post(
            f"/api/chat/sessions/{chat['session_id']}/messages",
            json={
                "agent_id": other_agent.json()["id"],
                "message": "This must not be persisted.",
            },
        )

        with Session(get_engine()) as session:
            after = (
                len(session.exec(select(ChatMessage)).all()),
                len(session.exec(select(AgentInvocation)).all()),
                len(session.exec(select(AgentExecution)).all()),
            )

    assert response.status_code == 422
    assert response.json()["detail"][0]["type"] == "extra_forbidden"
    assert after == before


def test_message_idempotency_replay_mismatch_and_transport_conflict():
    with TestClient(app) as client:
        chat = client.post(
            "/api/chat/sessions", json={"agent_id": "default-agent"}
        ).json()
        dispatcher = app.state.conversation_service.execution_dispatcher
        app.state.conversation_service.execution_dispatcher = None
        try:
            first = client.post(
                f"/api/chat/sessions/{chat['session_id']}/messages",
                headers={"Idempotency-Key": "api-key"},
                json={"message": "same payload", "message_id": "client-api-message"},
            )
            replay = client.post(
                f"/api/chat/sessions/{chat['session_id']}/messages",
                headers={"Idempotency-Key": "api-key"},
                json={"message": "same payload", "message_id": "client-api-message"},
            )
            mismatch = client.post(
                f"/api/chat/sessions/{chat['session_id']}/messages",
                headers={"Idempotency-Key": "api-key"},
                json={"message": "different payload", "message_id": "client-api-message"},
            )
            message_id_mismatch = client.post(
                f"/api/chat/sessions/{chat['session_id']}/messages",
                headers={"Idempotency-Key": "api-key"},
                json={"message": "same payload", "message_id": "different-message-id"},
            )
            conflict = client.post(
                f"/api/chat/sessions/{chat['session_id']}/messages",
                headers={"Idempotency-Key": "header-key"},
                json={"message": "transport conflict", "idempotency_key": "body-key"},
            )
        finally:
            app.state.conversation_service.execution_dispatcher = dispatcher

    assert first.status_code == replay.status_code == 202
    assert first.json()["message_id"] == replay.json()["message_id"]
    assert first.json()["execution_id"] == replay.json()["execution_id"]
    assert mismatch.status_code == message_id_mismatch.status_code == 409
    assert mismatch.json()["detail"]["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert message_id_mismatch.json()["detail"]["code"] == "IDEMPOTENCY_PAYLOAD_MISMATCH"
    assert conflict.status_code == 409
    assert conflict.json()["detail"]["code"] == "IDEMPOTENCY_KEY_CONFLICT"


def test_chat_run_completes_with_plain_reply():
    model = FakeModel([AIMessage(content="plain reply")])

    install_fake_model(model)

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()

        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={
                "message": "plain message",
            },
        )

        assert response.status_code == 202

        payload = wait_for_terminal_session(client, session["session_id"])

    assert payload["latest_execution"]["status"] == "completed"

    assert [message["role"] for message in payload["messages"]] == ["user", "assistant"]

    assert payload["messages"][-1]["content"] == "plain reply"


def test_session_pins_agent_version_across_later_turns():
    install_fake_model(FakeModel([AIMessage(content="pinned version reply")]))
    with TestClient(app) as client:
        created = client.post(
            "/api/chat/sessions",
            json={"agent_id": "default-agent"},
        )
        assert created.status_code == 200
        chat = created.json()
        assert chat["agent_version_id"] == "default-agent-v1"

        with Session(get_engine()) as session:
            session.add(
                AgentVersion(
                    id="default-agent-v2",
                    agent_id="default-agent",
                    version=2,
                    topic_scoring_prompt="new scoring rules",
                    content_prompt="new content rules",
                )
            )
            session.commit()

        submitted = client.post(
            f"/api/chat/sessions/{chat['session_id']}/messages",
            json={"message": "keep the pinned version"},
        )
        assert submitted.status_code == 202
        execution_id = submitted.json()["execution_id"]

        with Session(get_engine()) as session:
            execution = session.get(AgentExecution, execution_id)
            persisted_chat = session.get(ChatSession, chat["session_id"])
            assert execution is not None and persisted_chat is not None
            assert persisted_chat.agent_version_id == "default-agent-v1"
            assert execution.agent_version_id == "default-agent-v1"


def test_chat_message_route_runs_in_background():
    install_fake_model(FakeModel([AIMessage(content="background reply")]))

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()

        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={
                "message": "background message",
            },
        )

        assert response.status_code == 202

        assert response.json()["status"] == "pending"

        payload = wait_for_terminal_session(client, session["session_id"])

    assert payload["latest_execution"]["status"] == "completed"

    assert payload["messages"][-1]["content"] == "background reply"


def test_completed_turn_extracts_long_term_memory_with_model_judgement():
    install_fake_model(
        FakeModel([AIMessage(content="plain reply")]),
        memories=[
            {
                "kind": "preference",
                "content": "stable memory",
                "confidence": 0.92,
                "reason": "stable preference",
            }
        ],
    )

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()

        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={"message": "please remember this"},
        )

        assert response.status_code == 202

        payload = wait_for_terminal_session(client, session["session_id"])

    assert payload["latest_execution"]["status"] == "completed"
    with Session(get_engine()) as db_session:
        memories = db_session.exec(
            select(MemoryRecord).where(MemoryRecord.agent_id == "default-agent")
        ).all()
    assert any(item.content == "stable memory" for item in memories)


def test_memory_extraction_filters_low_confidence_and_sensitive_candidates():
    install_fake_model(
        FakeModel([AIMessage(content="plain reply")]),
        memories=[
            {
                "kind": "preference",
                "content": "temporary memory",
                "confidence": 0.3,
                "reason": "temporary",
            },
            {
                "kind": "credential",
                "content": "api_key: sk-1234567890abcdef",
                "confidence": 0.99,
                "reason": "sensitive",
            },
        ],
    )

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()

        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={"message": "temporary preference"},
        )

        assert response.status_code == 202

        payload = wait_for_terminal_session(client, session["session_id"])

    assert payload["latest_execution"]["status"] == "completed"
    with Session(get_engine()) as db_session:
        assert db_session.exec(
            select(MemoryRecord).where(MemoryRecord.agent_id == "default-agent")
        ).all() == []


def test_chat_session_title_is_generated_by_model():
    generated_title = "Deep finance title that is too long"

    install_fake_model(FakeModel([AIMessage(content="plain reply")]), title=generated_title)

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()

        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={
                "message": "Please generate a short title for this session",
            },
        )

        assert response.status_code == 202

        wait_for_terminal_session(client, session["session_id"])

        expected_title = generated_title.replace(" ", "")[:15]

        sessions = wait_for_session_title(client, expected_title=expected_title)

    assert sessions[0]["title"] != "New Session"

    assert sessions[0]["title"] == expected_title

    assert len(sessions[0]["title"]) <= 15


def test_postprocess_failure_keeps_main_run_completed_and_schedules_retry():
    message = "Long fallback title message for testing title generation"

    install_fake_model(
        FakeModel([AIMessage(content="plain reply")]),
        error=RuntimeError("title failed"),
    )

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()

        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={
                "message": message,
            },
        )

        assert response.status_code == 202

        execution_id = response.json()["execution_id"]
        terminal = wait_for_terminal_session(client, session["session_id"])

        sessions = client.get("/api/chat/sessions").json()["items"]

    assert terminal["latest_execution"]["status"] == "completed"
    assert sessions[0]["title"] == "New Session"
    with Session(get_engine()) as db_session:
        outbox = db_session.exec(
            select(ExecutionOutbox).where(
                ExecutionOutbox.execution_id == execution_id,
                ExecutionOutbox.kind == "postprocess",
            )
        ).first()
        assert outbox is not None
        assert outbox.status == "pending"
        assert outbox.processing_attempts == 1


def test_message_stream_returns_runtime_events():
    install_fake_model(FakeModel([AIMessage(content="streamed reply")]))

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()

        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={
                "message": "stream this reply",
            },
        )

        assert response.status_code == 202
        response = client.get(
            f"/api/chat/runs/{response.json()['execution_id']}/events",
        )

    assert response.status_code == 200

    assert "event: messages" in response.text

    assert '"schema_version":3' in response.text

    assert "event: lifecycle" in response.text

    assert '"event_id":"exe_' in response.text

    assert '"execution_id":"exe_' in response.text


def test_v3_stream_channels_classify_semantic_terminal_events() -> None:
    assert _stream_channel("token", "assistant_message_delta") == "messages"
    assert _stream_channel("state", "run_finish") == "lifecycle"
    assert _stream_channel("error", "run_error") == "errors"
    assert _stream_channel("state", "run_interrupt") == "interrupts"
    assert _stream_channel("tool_progress", "tool_progress") == "tools"


def test_stream_recovery_errors_keep_stable_degradation_codes() -> None:
    assert _stream_exception_payload(StreamingDegradedError("redis"))["code"] == (
        "STREAMING_DEGRADED"
    )
    assert _stream_exception_payload(StreamReplayGapError("gap"))["code"] == (
        "STREAM_REPLAY_GAP"
    )
    assert _stream_exception_payload(StreamReplayExpiredError("expired"))["code"] == (
        "STREAM_REPLAY_EXPIRED"
    )


def test_session_message_count_includes_persisted_conversation_messages():
    with TestClient(app) as client:
        created = client.post("/api/chat/sessions", json={"agent_id": "default-agent"})
        assert created.status_code == 200
        session_id = created.json()["session_id"]

    with Session(get_engine()) as session:
        session.add_all(
            [
                ChatMessage(
                    session_id=session_id,
                    role=MessageRole.user,
                    message_type=MessageType.text,
                    content="visible user message",
                ),
                ChatMessage(
                    session_id=session_id,
                    role=MessageRole.assistant,
                    message_type=MessageType.markdown,
                    content="visible assistant message",
                ),
            ]
        )
        session.commit()

    with TestClient(app) as client:
        summaries = client.get("/api/chat/sessions")
        detail = client.get(f"/api/chat/sessions/{session_id}")

    assert summaries.status_code == 200
    assert summaries.json()["items"][0]["session_id"] == session_id
    assert summaries.json()["items"][0]["message_count"] == 2
    assert detail.status_code == 200
    assert detail.json()["message_count"] == 2


def test_waiting_input_run_can_be_resumed():
    install_fake_model(
        FakeModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "name": "remember",
                            "args": {"content": "跨 Worker 恢复测试"},
                            "id": "call-resume-1",
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )
    )

    with TestClient(app) as client:
        chat = client.post(
            "/api/chat/sessions",
            json={"agent_id": "default-agent"},
        ).json()
        started = client.post(
            f"/api/chat/sessions/{chat['session_id']}/messages",
            json={"message": "请记住这项偏好"},
        )
        assert started.status_code == 202
        execution_id = started.json()["execution_id"]
        waiting = wait_for_terminal_session(client, chat["session_id"])
        assert waiting["latest_execution"]["status"] == "waiting_input"

        # Replace the whole runtime (including its PostgreSQL pool/checkpointer)
        # before resuming to prove that recovery does not depend on worker memory.
        app.state.agent_service.close()
        install_fake_model(
            FakeModel(
                [
                    AIMessage(content="resumed reply"),
                    AIMessage(content="resumed reply"),
                ]
            )
        )
        replacement_service = AgentService(app.state.settings, runtime=_TEST_RUNTIME)
        replacement_service.start()
        replacement_dispatcher = DirectDispatcher(replacement_service)
        app.state.agent_service = replacement_service
        app.state.conversation_service.agent_service = replacement_service
        app.state.conversation_service.execution_dispatcher = replacement_dispatcher

        response = client.post(
            f"/api/chat/runs/{execution_id}/resume",
            json={
                "interrupt_id": waiting["latest_execution"]["interrupt"]["interrupt_id"],
                "decision": "approve",
            },
        )

        assert response.status_code == 200
        response = client.get(f"/api/chat/runs/{execution_id}/events")

    assert response.status_code == 200

    assert "event: lifecycle" in response.text

    assert '"schema_version":3' in response.text

    assert '"execution_id":"exe_' in response.text

    assert '"name":"run_finish"' in response.text

    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)

        assert execution is not None

        assert execution.status == RunStatus.completed


def test_waiting_input_run_blocks_new_turn_until_resumed():
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
            agent_id="default-agent",
            user_id="local-user",
        )

        session.add(invocation)

        session.flush()

        message = ChatMessage(
            session_id=chat.id,
            invocation_id=invocation.id,
            role=MessageRole.user,
            content="Need human input",
        )

        session.add(message)

        session.flush()

        session.add(
            AgentExecution(
                invocation_id=invocation.id,
                session_id=chat.id,
                agent_version_id="default-agent-v1",
                status=RunStatus.waiting_input,
            )
        )

        session.commit()

        session_id = chat.id

    with TestClient(app) as client:
        blocked = client.post(
            f"/api/chat/sessions/{session_id}/messages",
            json={"message": "start a second turn"},
        )

    assert blocked.status_code == 409

    detail = blocked.json()["detail"]

    assert detail["code"] == "SESSION_HAS_ACTIVE_EXECUTION"

    assert detail["session_id"] == session_id

    assert "thread_id" not in detail

    with Session(get_engine()) as session:
        assert session.exec(select(ChatMessage).where(ChatMessage.session_id == session_id)).all()

        assert session.exec(
            select(AgentExecution)
            .join(AgentInvocation, AgentExecution.invocation_id == AgentInvocation.id)
            .where(AgentInvocation.session_id == session_id)
        ).all()

        assert (
            session.exec(
                select(ChatMessage).where(ChatMessage.content == "start a second turn")
            ).first()
            is None
        )


def test_chat_session_is_hard_deleted_with_related_rows():
    persistence = RuntimePersistence()

    persistence.start()

    persistence.close()

    with Session(get_engine()) as session:
        chat = ChatSession(
            title="Deletable session",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )

        session.add(chat)

        session.flush()

        invocation = AgentInvocation(
            session_id=chat.id,
            agent_id="default-agent",
            user_id="local-user",
        )

        session.add(invocation)

        session.flush()

        message = ChatMessage(
            session_id=chat.id,
            invocation_id=invocation.id,
            role=MessageRole.user,
            content="delete this session",
        )

        session.add(message)

        session.flush()

        execution = AgentExecution(
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id="default-agent-v1",
            status=RunStatus.completed,
        )

        session.add(invocation)

        session.add(execution)

        session.add(
            MemoryRecord(
                user_id=chat.user_id,
                session_id=chat.id,
                memory_key="summary",
                kind="summary",
                content="summary",
                payload={"scope": "thread"},
            )
        )

        session.execute(
            text(
                """

                INSERT INTO checkpoints (

                    thread_id, checkpoint_ns, checkpoint_id, checkpoint, metadata

                )

                VALUES (:thread_id, '', 'chk_test', '{}'::jsonb, '{}'::jsonb)

                ON CONFLICT DO NOTHING

                """
            ),
            {"thread_id": chat.langgraph_thread_id},
        )

        session.execute(
            text(
                """

                INSERT INTO checkpoint_blobs (

                    thread_id, checkpoint_ns, channel, version, type, blob

                )

                VALUES (:thread_id, '', 'messages', '1', 'empty', NULL)

                ON CONFLICT DO NOTHING

                """
            ),
            {"thread_id": chat.langgraph_thread_id},
        )

        session.execute(
            text(
                """

                INSERT INTO checkpoint_writes (

                    thread_id, checkpoint_ns, checkpoint_id, task_id, task_path,

                    idx, channel, type, blob

                )

                VALUES (

                    :thread_id, '', 'chk_test', 'task_test', '', 0, 'messages',

                    'msgpack', :blob

                )

                ON CONFLICT DO NOTHING

                """
            ),
            {"thread_id": chat.langgraph_thread_id, "blob": b"stale"},
        )

        session.commit()

        session_id = chat.id

        thread_id = chat.langgraph_thread_id

        invocation_id = invocation.id

        execution_id = execution.id

    with TestClient(app) as client:
        response = client.delete(f"/api/chat/sessions/{session_id}")

        assert response.status_code == 204

        assert client.get(f"/api/chat/sessions/{session_id}").status_code == 404

    with Session(get_engine()) as session:
        assert session.get(ChatSession, session_id) is None
        assert (
            session.exec(select(ChatMessage).where(ChatMessage.session_id == session_id)).first()
            is None
        )
        assert session.get(AgentInvocation, invocation_id) is None
        assert session.get(AgentExecution, execution_id) is None

        assert (
            session.exec(
                select(MemoryRecord).where(
                    MemoryRecord.session_id == session_id,
                )
            ).first()
            is None
        )

        tombstone = session.exec(
            select(AdminAuditLog).where(AdminAuditLog.action == "session.deleted")
        ).one()
        assert "session_hash" in tombstone.detail
        assert session_id not in str(tombstone.detail)

        assert (
            session.execute(
                text("SELECT 1 FROM checkpoints WHERE thread_id = :thread_id"),
                {"thread_id": thread_id},
            ).first()
            is None
        )

        assert (
            session.execute(
                text("SELECT 1 FROM checkpoint_blobs WHERE thread_id = :thread_id"),
                {"thread_id": thread_id},
            ).first()
            is None
        )

        assert (
            session.execute(
                text("SELECT 1 FROM checkpoint_writes WHERE thread_id = :thread_id"),
                {"thread_id": thread_id},
            ).first()
            is None
        )


def test_chat_session_delete_keeps_rows_when_persistence_cleanup_fails(monkeypatch):
    with Session(get_engine()) as session:
        chat = ChatSession(
            title="cleanup failure",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )

        session.add(chat)

        session.commit()

        session_id = chat.id

    def fail_cleanup(*, thread_id: str, session: Session | None = None) -> None:
        raise RuntimeError("cleanup failed")

    monkeypatch.setattr(
        conversation_service_module,
        "clear_thread_persistence",
        fail_cleanup,
    )

    with TestClient(app) as client:
        response = client.delete(f"/api/chat/sessions/{session_id}")

    assert response.status_code == 500

    with Session(get_engine()) as session:
        assert session.get(ChatSession, session_id) is not None


def test_chat_session_delete_rejects_active_run():
    with Session(get_engine()) as session:
        chat = ChatSession(
            title="Running session",
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )

        session.add(chat)

        session.flush()

        invocation = AgentInvocation(
            session_id=chat.id,
            agent_id="default-agent",
            user_id="local-user",
        )

        session.add(invocation)

        session.flush()

        session.add(
            AgentExecution(
                invocation_id=invocation.id,
                session_id=chat.id,
                agent_version_id="default-agent-v1",
                status=RunStatus.running,
            )
        )

        session.commit()

        session_id = chat.id

    with TestClient(app) as client:
        response = client.delete(f"/api/chat/sessions/{session_id}")

        assert response.status_code == 409

        assert client.get(f"/api/chat/sessions/{session_id}").status_code == 200


def test_chat_session_delete_unknown_session_returns_404():
    with TestClient(app) as client:
        response = client.delete("/api/chat/sessions/ses_missing")

    assert response.status_code == 404


def test_chat_run_can_call_memory_tool():
    install_fake_model(
        FakeModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call_1",
                            "name": "remember",
                            "args": {
                                "content": "prefers concise replies",
                                "kind": "preference",
                            },
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="remembered"),
            ]
        )
    )

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()

        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={
                "message": "please remember my preference",
            },
        )

        assert response.status_code == 202

        payload = wait_for_terminal_session(client, session["session_id"])
        assert payload["latest_execution"]["status"] == "waiting_input"
        resumed = client.post(
            f"/api/chat/runs/{response.json()['execution_id']}/resume",
            json={
                "interrupt_id": payload["latest_execution"]["interrupt"]["interrupt_id"],
                "decision": "approve",
            },
        )
        assert resumed.status_code == 200
        payload = wait_for_terminal_session(client, session["session_id"])

    assert payload["latest_execution"]["status"] == "completed"

    assert all(message["role"] in {"user", "assistant"} for message in payload["messages"])

    with Session(get_engine()) as db_session:
        memories = db_session.exec(
            select(MemoryRecord).where(MemoryRecord.agent_id == "default-agent")
        ).all()
    assert any(item.content == "prefers concise replies" for item in memories)


def test_remember_approval_projection_matches_exact_saved_normalization():
    raw_content = json.dumps(
        {"content": "  " + ("durable preference " * 100) + "  "},
        ensure_ascii=False,
    )
    install_fake_model(
        FakeModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-normalized-remember",
                            "name": "remember",
                            "args": {
                                "content": raw_content,
                                "kind": "NOT-AN-ALLOWED-KIND" * 10,
                            },
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="saved normalized memory"),
            ]
        )
    )

    with TestClient(app) as client:
        chat = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()
        started = client.post(
            f"/api/chat/sessions/{chat['session_id']}/messages",
            json={"message": "remember normalized content"},
        )
        waiting = wait_for_terminal_session(client, chat["session_id"])
        interrupt = waiting["latest_execution"]["interrupt"]
        projected_memory = interrupt["actions"][0]["memory"]
        assert projected_memory["type"] == "semantic"
        assert len(projected_memory["content"]) == 1000

        resumed = client.post(
            f"/api/chat/runs/{started.json()['execution_id']}/resume",
            json={
                "interrupt_id": interrupt["interrupt_id"],
                "decision": "approve",
            },
        )
        assert resumed.status_code == 200
        terminal = wait_for_terminal_session(client, chat["session_id"])
        assert terminal["latest_execution"]["status"] == "completed"

    with Session(get_engine()) as db_session:
        saved = db_session.exec(
            select(MemoryRecord).where(
                MemoryRecord.source_execution_id == started.json()["execution_id"],
                MemoryRecord.content == projected_memory["content"],
            )
        ).all()
        assert len(saved) == 1
        assert saved[0].kind == projected_memory["type"]


def test_reject_resumes_original_namespace_without_executing_pending_tool():
    install_fake_model(
        FakeModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-reject",
                            "name": "remember",
                            "args": {"content": "must not be saved", "kind": "preference"},
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )
    )

    with TestClient(app) as client:
        chat = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()
        started = client.post(
            f"/api/chat/sessions/{chat['session_id']}/messages",
            json={"message": "do not save this"},
        )
        waiting = wait_for_terminal_session(client, chat["session_id"])
        interrupt_id = waiting["latest_execution"]["interrupt"]["interrupt_id"]

        rejected = client.post(
            f"/api/chat/runs/{started.json()['execution_id']}/resume",
            json={"interrupt_id": interrupt_id, "decision": "reject"},
        )
        assert rejected.status_code == 200
        terminal = wait_for_terminal_session(client, chat["session_id"])

    assert terminal["latest_execution"]["status"] == "completed"
    assert terminal["messages"][-1]["content"] == "已取消保存"
    assert [message["content"] for message in terminal["messages"]].count(
        "已拒绝工具执行。"
    ) == 1
    assistant_messages = [
        message for message in terminal["messages"] if message["role"] == "assistant"
    ]
    assert len(assistant_messages) == 1
    assert assistant_messages[0]["content"] == "已取消保存"
    with Session(get_engine()) as db_session:
        assert db_session.exec(
            select(MemoryRecord).where(MemoryRecord.content == "must not be saved")
        ).all() == []


def test_cancel_waiting_interrupt_makes_resume_stale_and_executes_no_tool():
    install_fake_model(
        FakeModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-cancel",
                            "name": "remember",
                            "args": {"content": "cancelled memory"},
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )
    )

    with TestClient(app) as client:
        chat = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()
        started = client.post(
            f"/api/chat/sessions/{chat['session_id']}/messages",
            json={"message": "cancel this tool"},
        )
        waiting = wait_for_terminal_session(client, chat["session_id"])
        interrupt_id = waiting["latest_execution"]["interrupt"]["interrupt_id"]
        cancelled = client.post(f"/api/chat/runs/{started.json()['execution_id']}/cancel")
        stale = client.post(
            f"/api/chat/runs/{started.json()['execution_id']}/resume",
            json={"interrupt_id": interrupt_id, "decision": "approve"},
        )

    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "cancelled"
    assert cancelled.json()["interrupt"] is None
    assert stale.status_code == 409
    assert stale.json()["detail"]["code"] == "RUN_INTERRUPT_STALE"
    with Session(get_engine()) as db_session:
        assert db_session.exec(
            select(MemoryRecord).where(MemoryRecord.content == "cancelled memory")
        ).all() == []
        outbox = db_session.exec(
            select(ExecutionOutbox).where(
                ExecutionOutbox.execution_id == started.json()["execution_id"],
                ExecutionOutbox.kind == "execute",
            )
        ).one()
        assert outbox.status == "cancelled"


def _unsafe_remember_interrupts(content: str) -> list[dict[str, Any]]:
    return [
        {
            "id": "int-unsafe-status",
            "value": {
                "tool_calls": [
                    {
                        "name": "remember",
                        "args": {"content": content, "kind": "preference"},
                    }
                ]
            },
        }
    ]


@pytest.mark.parametrize(
    ("interrupts_payload", "forbidden_fragment"),
    [
        (
            [
                {
                    "id": "int-unsafe-status",
                    "value": {
                        "tool_calls": [
                            {"name": "x" * 256, "args": {"token": "secret-long-name"}}
                        ]
                    },
                }
            ],
            "secret-long-name",
        ),
        (
            [
                {
                    "id": "int-unsafe-status",
                    "value": {
                        "tool_calls": [
                            {
                                "name": "remember",
                                "args": {"kind": "preference", "api_key": "secret"},
                            }
                        ]
                    },
                }
            ],
            "secret",
        ),
        (
            [
                {
                    "id": "int-unsafe-status",
                    "value": {
                        "tool_calls": [
                            {"name": {"malformed": True}, "args": {"password": "secret"}}
                        ]
                    },
                }
            ],
            "secret",
        ),
        (
            [
                {
                    "id": "int-unsafe-status",
                    "value": {
                        "tool_calls": [
                            {
                                "name": "remember",
                                "args": {"content": "safe memory", "kind": None},
                            }
                        ]
                    },
                }
            ],
            "safe memory",
        ),
        (
            [
                {
                    "id": "int-unsafe-status",
                    "value": {
                        "tool_calls": [
                            {
                                "name": "remember",
                                "args": {
                                    "content": "my API     key is super-private-value",
                                    "kind": "preference",
                                },
                            }
                        ]
                    },
                }
            ],
            "super-private-value",
        ),
        (
            _unsafe_remember_interrupts("OPENAI_API_KEY=opaque-api-status"),
            "opaque-api-status",
        ),
        (
            _unsafe_remember_interrupts(
                "google_client_secret = opaque-client-status"
            ),
            "opaque-client-status",
        ),
        (
            _unsafe_remember_interrupts("GitHub-Access-Token: opaque-access-status"),
            "opaque-access-status",
        ),
        (
            _unsafe_remember_interrupts("RSA PRIVATE KEY = opaque-private-status"),
            "opaque-private-status",
        ),
        (
            _unsafe_remember_interrupts("googleClientSecret=opaque-camel-status"),
            "opaque-camel-status",
        ),
        (
            [
                {
                    "id": "int-unsafe-status",
                    "value": {
                        "tool_calls": [
                            {"name": "remember", "args": {"content": "visible first"}}
                        ]
                    },
                },
                {
                    "id": "int-hidden-status",
                    "value": {
                        "tool_calls": [
                            {"name": "remember", "args": {"content": "hidden second"}}
                        ]
                    },
                },
            ],
            "hidden second",
        ),
    ],
)
def test_unsafe_waiting_interrupt_status_is_total_resume_is_stale_and_cancel_works(
    interrupts_payload: list[dict[str, Any]],
    forbidden_fragment: str,
    caplog: pytest.LogCaptureFixture,
):
    caplog.set_level(logging.DEBUG)
    with Session(get_engine()) as db_session:
        chat = ChatSession(
            agent_id="default-agent",
            agent_version_id="default-agent-v1",
            user_id="local-user",
        )
        db_session.add(chat)
        db_session.flush()
        invocation = AgentInvocation(
            session_id=chat.id,
            agent_id=chat.agent_id,
            user_id=chat.user_id,
        )
        db_session.add(invocation)
        db_session.flush()
        execution = AgentExecution(
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id=chat.agent_version_id,
            status=RunStatus.waiting_input,
            interrupt_payload={
                "interrupts": interrupts_payload,
                "runtime": {
                    "execution_id": "secret-execution",
                    "api_key": "secret-runtime",
                },
            },
        )
        db_session.add(execution)
        db_session.commit()
        execution_id = execution.id

    with TestClient(app) as client:
        status_response = client.get(f"/api/chat/runs/{execution_id}/status")
        assert status_response.status_code == 200
        assert status_response.json()["interrupt"] is None
        encoded_status = json.dumps(status_response.json(), ensure_ascii=False)
        assert forbidden_fragment not in encoded_status
        assert forbidden_fragment not in caplog.text

        stale = client.post(
            f"/api/chat/runs/{execution_id}/resume",
            json={"interrupt_id": "int-unsafe-status", "decision": "approve"},
        )
        assert stale.status_code == 409
        assert stale.json()["detail"]["code"] == "RUN_INTERRUPT_STALE"

        cancelled = client.post(f"/api/chat/runs/{execution_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["interrupt"] is None
        encoded_flow = json.dumps(
            [status_response.json(), stale.json(), cancelled.json()],
            ensure_ascii=False,
        )
        assert forbidden_fragment not in encoded_flow
        assert forbidden_fragment not in caplog.text


def test_wrong_interrupt_id_is_stale_and_status_interrupt_is_allowlisted():
    install_fake_model(
        FakeModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-private-status",
                            "name": "remember",
                            "args": {
                                "content": "public memory content",
                                "kind": "preference",
                                "api_key": "secret",
                            },
                            "type": "tool_call",
                        },
                        {
                            "id": "call-private-second",
                            "name": "recall_memory",
                            "args": {
                                "query": "second action must be visible",
                                "access_token": "second-secret",
                            },
                            "type": "tool_call",
                        },
                    ],
                )
            ]
        )
    )

    with TestClient(app) as client:
        chat = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()
        started = client.post(
            f"/api/chat/sessions/{chat['session_id']}/messages",
            json={"message": "inspect approval"},
        )
        waiting = wait_for_terminal_session(client, chat["session_id"])
        public_interrupt = waiting["latest_execution"]["interrupt"]
        wrong = client.post(
            f"/api/chat/runs/{started.json()['execution_id']}/resume",
            json={"interrupt_id": "wrong-id", "decision": "approve"},
        )

    assert set(public_interrupt) == {"interrupt_id", "actions"}
    assert public_interrupt["actions"] == [
        {
            "tool_name": "remember",
            "purpose": "保存一条长期记忆",
            "memory": {
                "type": "preference",
                "content": "public memory content",
            },
        },
        {
            "tool_name": "recall_memory",
            "purpose": "运行工具 recall_memory",
            "memory": None,
        },
    ]
    encoded = json.dumps(public_interrupt, ensure_ascii=False)
    assert "call-private-status" not in encoded
    assert "call-private-second" not in encoded
    assert "api_key" not in encoded
    assert "secret" not in encoded
    assert wrong.status_code == 409
    assert wrong.json()["detail"]["code"] == "RUN_INTERRUPT_STALE"


@pytest.mark.parametrize("tampered_hash", ["", "short", "g" * 64, "0" * 64])
def test_claim_rejects_tampered_tool_call_hash_as_stable_stale_interrupt(
    tampered_hash: str,
):
    install_fake_model(
        FakeModel(
            [
                AIMessage(
                    content="",
                    tool_calls=[
                        {
                            "id": "call-hash-tamper",
                            "name": "remember",
                            "args": {"content": "hash protected memory"},
                            "type": "tool_call",
                        }
                    ],
                )
            ]
        )
    )

    with TestClient(app) as client:
        chat = client.post("/api/chat/sessions", json={"agent_id": "default-agent"}).json()
        started = client.post(
            f"/api/chat/sessions/{chat['session_id']}/messages",
            json={"message": "protect this approval"},
        )
        waiting = wait_for_terminal_session(client, chat["session_id"])
        interrupt_id = waiting["latest_execution"]["interrupt"]["interrupt_id"]
        dispatcher = app.state.conversation_service.execution_dispatcher
        app.state.conversation_service.execution_dispatcher = None
        try:
            resumed = client.post(
                f"/api/chat/runs/{started.json()['execution_id']}/resume",
                json={"interrupt_id": interrupt_id, "decision": "approve"},
            )
        finally:
            app.state.conversation_service.execution_dispatcher = dispatcher
        assert resumed.status_code == 200

        with Session(get_engine()) as db_session:
            request = db_session.exec(
                select(ExecutionResumeRequest).where(
                    ExecutionResumeRequest.execution_id == started.json()["execution_id"]
                )
            ).one()
            request.tool_calls_hash = tampered_hash
            db_session.add(request)
            db_session.commit()

        claimed = claim_execution(
            app.state.agent_service,
            started.json()["execution_id"],
            "hash-tamper-worker",
            use_lease=False,
        )

    assert claimed is None
    with Session(get_engine()) as db_session:
        execution = db_session.get(AgentExecution, started.json()["execution_id"])
        request = db_session.exec(
            select(ExecutionResumeRequest).where(
                ExecutionResumeRequest.execution_id == started.json()["execution_id"]
            )
        ).one()
        assert execution is not None
        assert execution.status == RunStatus.failed
        assert execution.error == "RUN_INTERRUPT_STALE"
        assert request.status == "stale"


@pytest.mark.parametrize(
    "fault_window",
    [
        "before_first_checkpoint",
        "after_end",
        "after_assistant_flush",
        "before_terminal_commit",
    ],
)
def test_fault_windows_redeliver_three_times_and_converge_exactly_once(
    fault_window: str,
):
    class _CrashPersister(MessagePersister):
        def __init__(self, *, after_flush: bool) -> None:
            self.after_flush = after_flush
            self.crashes_remaining = 2

        def persist_graph_messages(self, *args: Any, **kwargs: Any) -> ChatMessage | None:
            if not self.after_flush and self.crashes_remaining:
                self.crashes_remaining -= 1
                raise SystemExit("fault after END")
            message = super().persist_graph_messages(*args, **kwargs)
            if self.after_flush and self.crashes_remaining:
                self.crashes_remaining -= 1
                raise SystemExit("fault after Assistant flush")
            return message

    class _CountingWriter:
        def __init__(self) -> None:
            self.events: list[tuple[str, dict[str, Any]]] = []

        def emit(self, event_name: str, payload: dict[str, Any]) -> None:
            self.events.append((event_name, dict(payload)))

    memory_content = f"fault-side-effect-{fault_window}"
    model = FakeModel(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "id": f"call-{fault_window}",
                        "name": "remember",
                        "args": {"content": memory_content, "kind": "preference"},
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="checkpoint final"),
        ]
    )
    install_fake_model(model)
    event_writer = _CountingWriter()

    with TestClient(app) as client:
        dispatcher = app.state.conversation_service.execution_dispatcher
        app.state.conversation_service.execution_dispatcher = None
        service = app.state.agent_service
        original_max_attempts = service.settings.agent.max_execution_attempts
        original_persister = service.runner.execution_engine.message_persister
        original_run_turn = service.runner.execution_engine.run_turn
        service.settings.agent.max_execution_attempts = 6
        try:
            chat = client.post(
                "/api/chat/sessions", json={"agent_id": "default-agent"}
            ).json()
            started = client.post(
                f"/api/chat/sessions/{chat['session_id']}/messages",
                json={"message": f"fault window {fault_window}"},
            )
            execution_id = started.json()["execution_id"]
            initial_claim = claim_execution(
                service,
                execution_id,
                f"fault-setup-{fault_window}",
                use_lease=False,
            )
            assert initial_claim is not None
            with Session(get_engine()) as setup_session:
                service.runner.run(
                    setup_session,
                    execution_id=execution_id,
                    auth=initial_claim.auth,
                    tool_permissions=initial_claim.auth.tool_permissions,
                    event_writer=event_writer,
                )
            waiting = client.get(f"/api/chat/runs/{execution_id}/status").json()
            assert waiting["status"] == "waiting_input"
            interrupt_id = waiting["interrupt"]["interrupt_id"]
            resumed = client.post(
                f"/api/chat/runs/{execution_id}/resume",
                json={"interrupt_id": interrupt_id, "decision": "approve"},
            )
            assert resumed.status_code == 200

            crashes_before_checkpoint = 2

            def crash_before_checkpoint(**kwargs: Any) -> Any:
                nonlocal crashes_before_checkpoint
                if crashes_before_checkpoint:
                    crashes_before_checkpoint -= 1
                    raise SystemExit("fault before first resumed checkpoint")
                return original_run_turn(**kwargs)

            if fault_window == "before_first_checkpoint":
                service.runner.execution_engine.run_turn = crash_before_checkpoint
            elif fault_window in {"after_end", "after_assistant_flush"}:
                service.runner.execution_engine.message_persister = _CrashPersister(
                    after_flush=fault_window == "after_assistant_flush"
                )

            real_deliveries = 0
            for delivery in range(3):
                claimed = claim_execution(
                    service,
                    execution_id,
                    f"fault-worker-{delivery}",
                    use_lease=False,
                )
                assert claimed is not None
                real_deliveries += 1
                crashed = False
                with Session(get_engine()) as db_session:
                    if fault_window == "before_terminal_commit" and delivery < 2:
                        def crash_before_terminal_commit(session: Any) -> None:
                            execution = session.get(AgentExecution, execution_id)
                            if execution is not None and execution.status == RunStatus.completed:
                                raise SystemExit("fault before terminal commit")

                        event.listen(db_session, "before_commit", crash_before_terminal_commit)
                    try:
                        service.runner.run(
                            db_session,
                            execution_id=execution_id,
                            auth=claimed.auth,
                            tool_permissions=claimed.auth.tool_permissions,
                            resume_value=claimed.resume_value,
                            resume_request_id=claimed.resume_request_id,
                            continue_from_checkpoint=claimed.continue_from_checkpoint,
                            event_writer=event_writer,
                        )
                    except SystemExit:
                        crashed = True
                    finally:
                        if fault_window == "before_terminal_commit" and delivery < 2:
                            event.remove(
                                db_session,
                                "before_commit",
                                crash_before_terminal_commit,
                            )
                if delivery < 2:
                    assert crashed
                    with Session(get_engine()) as recovery_session:
                        execution = recovery_session.get(AgentExecution, execution_id)
                        assert execution is not None
                        execution.status = RunStatus.pending
                        execution.worker_id = None
                        execution.claimed_at = None
                        execution.heartbeat_at = None
                        execution.lease_expires_at = None
                        if execution.current_attempt_id:
                            attempt = recovery_session.get(
                                AgentExecutionAttempt,
                                execution.current_attempt_id,
                            )
                            if attempt is not None and attempt.finished_at is None:
                                attempt.status = ExecutionAttemptStatus.lease_lost
                                attempt.finished_at = utcnow()
                                recovery_session.add(attempt)
                        recovery_session.add(execution)
                        recovery_session.commit()
                else:
                    assert not crashed
            assert real_deliveries == 3
        finally:
            service.runner.execution_engine.run_turn = original_run_turn
            service.runner.execution_engine.message_persister = original_persister
            service.settings.agent.max_execution_attempts = original_max_attempts
            app.state.conversation_service.execution_dispatcher = dispatcher

    with Session(get_engine()) as db_session:
        execution = db_session.get(AgentExecution, execution_id)
        assistants = db_session.exec(
            select(ChatMessage).where(
                ChatMessage.execution_id == execution_id,
                ChatMessage.role == MessageRole.assistant,
            )
        ).all()
        resume_requests = db_session.exec(
            select(ExecutionResumeRequest).where(
                ExecutionResumeRequest.execution_id == execution_id
            )
        ).all()
        attempts = db_session.exec(
            select(AgentExecutionAttempt)
            .where(AgentExecutionAttempt.execution_id == execution_id)
            .order_by(AgentExecutionAttempt.ordinal)
        ).all()
        tool_executions = db_session.exec(
            select(ToolExecution).where(ToolExecution.execution_id == execution_id)
        ).all()
        outboxes = db_session.exec(
            select(ExecutionOutbox).where(
                ExecutionOutbox.execution_id == execution_id,
                ExecutionOutbox.kind == "execute",
            )
        ).all()
        memories = db_session.exec(
            select(MemoryRecord).where(MemoryRecord.content == memory_content)
        ).all()
        assert execution is not None
        assert execution.status == RunStatus.completed
        assert execution.interrupt_payload == {}
        assert len(assistants) == 1
        assert assistants[0].content == "checkpoint final"
        assert len(resume_requests) == 1
        assert resume_requests[0].status == "consumed"
        assert resume_requests[0].consumed_at is not None
        assert len(attempts) == 4
        assert all(attempt.finished_at is not None for attempt in attempts)
        assert [attempt.status for attempt in attempts] == [
            ExecutionAttemptStatus.waiting_input,
            ExecutionAttemptStatus.lease_lost,
            ExecutionAttemptStatus.lease_lost,
            ExecutionAttemptStatus.completed,
        ]
        assert len(tool_executions) == 1
        assert tool_executions[0].status.value == "completed"
        assert len(memories) == 1
        assert len(outboxes) == 1
        chat_row = db_session.get(ChatSession, execution.session_id)
        assert chat_row is not None
        checkpoint_thread_id = chat_row.langgraph_thread_id
    assert checkpoint_interrupts(
        service.runtime.get_checkpointer(),
        thread_id=checkpoint_thread_id,
        execution_id=execution_id,
    ) == []
    event_names = [name for name, _payload in event_writer.events]
    assert event_names.count("tool_end") == 1
    assert event_names.count("assistant_message") == 1
    assert event_names.count("message_finish") == 1
    assert event_names.count("run_finish") == 1


def test_running_run_can_be_cancel_requested():
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
            agent_id="default-agent",
            user_id="local-user",
        )

        session.add(invocation)

        session.flush()

        execution = AgentExecution(
            invocation_id=invocation.id,
            session_id=chat.id,
            agent_version_id="default-agent-v1",
            status=RunStatus.running,
        )

        session.add(execution)

        session.commit()

        execution_id = execution.id

    with TestClient(app) as client:
        cancelled = client.post(f"/api/chat/runs/{execution_id}/cancel")

    assert cancelled.status_code == 200

    assert cancelled.json()["cancel_requested_at"] is not None
