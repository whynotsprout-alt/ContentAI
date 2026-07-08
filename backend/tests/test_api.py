import base64
import json
import time
from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any

import services.conversation_service as conversation_service_module
from agent.graph.factory import build_agent_graph
from agent.runtime.checkpoint import (
    build_checkpointer,
    build_store,
    close_runtime_persistence,
)
from agent.runtime.container import RuntimeContainer, set_runtime_container
from client import ApiClient as TestClient
from core.config import Settings
from db.session import get_engine
from langchain_core.messages import AIMessage
from main import app, create_app
from models.account import Account
from models.chat import AgentExecution, AgentInvocation, ChatMessage, ChatSession
from models.enums import MemoryScope, MessageRole, RunStatus
from models.memory import MemoryRecord
from sqlalchemy import text
from sqlmodel import Session, select


def unsigned_token(payload: dict[str, Any]) -> str:
    def encode(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode("utf-8")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none', 'typ': 'JWT'})}.{encode(payload)}."


def auth_headers(
    *,
    user_id: str = "user_1",
    tenant_id: str = "tenant_1",
    accounts: list[str] | None = None,
    tools: list[str] | None = None,
) -> dict[str, str]:
    token = unsigned_token(
        {
            "sub": user_id,
            "tenant_id": tenant_id,
            "allowed_account_ids": accounts if accounts is not None else ["*"],
            "tool_permissions": tools if tools is not None else ["*"],
        }
    )
    return {"Authorization": f"Bearer {token}"}


def auth_test_app():
    return create_app(
        Settings(
            database={"url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"},
            auth={"enabled": True, "allow_unsigned_test_tokens": True},
        )
    )


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


def install_fake_model(
    model: FakeModel,
    *,
    memories: list[dict[str, Any]] | None = None,
    title: str = "Generated Title",
    error: Exception | None = None,
) -> None:
    close_runtime_persistence()
    gateway = FakeGateway(title=title, error=error)
    gateway.model = model
    gateway.memories = memories or []
    container = RuntimeContainer(model_gateway=gateway)
    container.graph = build_agent_graph(
        model=model,
        tools=container.tools,
        checkpointer=container.checkpointer,
        store=container.store,
    )
    set_runtime_container(container)


def wait_for_terminal_session(client: TestClient, session_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/chat/sessions/{session_id}")
        assert response.status_code == 200
        payload = response.json()
        status = (payload.get("latest_execution") or {}).get("status")
        if status in {"completed", "failed", "cancelled", "interrupted"}:
            return payload
        time.sleep(0.1)
    raise AssertionError(f"run did not finish in time: {payload}")


def wait_for_session_title(client: TestClient, *, expected_title: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 5
    sessions: list[dict[str, Any]] = []
    while time.monotonic() < deadline:
        response = client.get("/api/chat/sessions")
        assert response.status_code == 200
        sessions = response.json()
        if sessions and sessions[0]["title"] == expected_title:
            return sessions
        time.sleep(0.1)
    raise AssertionError(f"session title did not update in time: {sessions}")


def test_account_crud_contract():
    payload = {
        "name": "Test Account",
        "positioning": "Account for API contract tests.",
        "topic_scoring_prompt": "Score topics from 0 to 100.",
        "content_creation_prompt": "Write a concise title and body.",
        "hotspot_sources": ["douyin", "weibo"],
    }

    with TestClient(app) as client:
        created = client.post("/api/accounts", json=payload)
        assert created.status_code == 201
        account_id = created.json()["id"]
        assert created.json()["positioning"] == payload["positioning"]
        assert created.json()["hotspot_sources"] == payload["hotspot_sources"]

        updated = client.patch(
            f"/api/accounts/{account_id}",
            json={
                "name": "婵犵數鍋炲娆擃敄閸儲鍎婃い鏍ㄧ矋鐎氬鏌ㄩ弴妤€浜鹃悷?2",
                "hotspot_sources": ["xiaohongshu"],
            },
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "婵犵數鍋炲娆擃敄閸儲鍎婃い鏍ㄧ矋鐎氬鏌ㄩ弴妤€浜鹃悷?2"
        assert updated.json()["hotspot_sources"] == ["xiaohongshu"]

        deleted = client.delete(f"/api/accounts/{account_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/accounts/{account_id}").status_code == 404


def test_account_names_are_unique_within_tenant_only():
    payload = {
        "name": "Shared Account",
        "positioning": "Account for tenant uniqueness tests.",
        "topic_scoring_prompt": "Score topics from 0 to 100.",
        "content_creation_prompt": "Write a concise title and body.",
        "hotspot_sources": ["douyin", "weibo"],
    }

    with TestClient(auth_test_app()) as client:
        created = client.post(
            "/api/accounts",
            headers=auth_headers(tenant_id="tenant_a"),
            json=payload,
        )
        duplicate = client.post(
            "/api/accounts",
            headers=auth_headers(tenant_id="tenant_a"),
            json=payload,
        )
        other_tenant = client.post(
            "/api/accounts",
            headers=auth_headers(tenant_id="tenant_b"),
            json=payload,
        )

    assert created.status_code == 201
    assert duplicate.status_code == 409
    assert other_tenant.status_code == 201

    with Session(get_engine()) as session:
        rows = session.exec(
            select(Account.tenant_id).where(Account.name == payload["name"])
        ).all()
    assert sorted(rows) == ["tenant_a", "tenant_b"]


def test_accounts_are_isolated_by_tenant():
    payload = {
        "name": "Tenant A Account",
        "positioning": "Tenant-scoped account.",
        "topic_scoring_prompt": "Score topics from 0 to 100.",
        "content_creation_prompt": "Write a concise title and body.",
        "hotspot_sources": ["weibo"],
    }

    with TestClient(auth_test_app()) as client:
        created = client.post(
            "/api/accounts",
            headers=auth_headers(tenant_id="tenant_a"),
            json=payload,
        )
        assert created.status_code == 201
        account_id = created.json()["id"]

        tenant_a_list = client.get("/api/accounts", headers=auth_headers(tenant_id="tenant_a"))
        tenant_b_list = client.get("/api/accounts", headers=auth_headers(tenant_id="tenant_b"))
        tenant_b_get = client.get(
            f"/api/accounts/{account_id}",
            headers=auth_headers(tenant_id="tenant_b"),
        )
        tenant_b_update = client.patch(
            f"/api/accounts/{account_id}",
            headers=auth_headers(tenant_id="tenant_b"),
            json={"name": "Blocked Rename"},
        )
        tenant_b_delete = client.delete(
            f"/api/accounts/{account_id}",
            headers=auth_headers(tenant_id="tenant_b"),
        )

    assert [account["id"] for account in tenant_a_list.json()] == [account_id]
    assert tenant_b_list.json() == []
    assert tenant_b_get.status_code == 404
    assert tenant_b_update.status_code == 404
    assert tenant_b_delete.status_code == 404


def test_account_put_not_allowed():
    payload = {
        "name": "Legacy Put Test",
        "positioning": "Account for method compatibility tests.",
        "topic_scoring_prompt": "Score topics from 0 to 100.",
        "content_creation_prompt": "Write a concise title and body.",
        "hotspot_sources": ["douyin", "weibo"],
    }

    with TestClient(app) as client:
        created = client.post("/api/accounts", json=payload)
        assert created.status_code == 201
        account_id = created.json()["id"]
        not_allowed = client.put(
            f"/api/accounts/{account_id}",
            json={"name": "Should not work"},
        )

    assert not_allowed.status_code == 405


def test_patch_account_partial_updates_are_supported():
    payload = {
        "name": "Original Account",
        "positioning": "Original positioning.",
        "topic_scoring_prompt": "Score topics from 0 to 100.",
        "content_creation_prompt": "Write a concise title and body.",
        "hotspot_sources": ["douyin", "weibo"],
    }

    with TestClient(app) as client:
        created = client.post("/api/accounts", json=payload)
        assert created.status_code == 201
        account_id = created.json()["id"]
        partial = client.patch(
            f"/api/accounts/{account_id}",
            json={"name": "Original Account Updated"},
        )
        after_partial = client.get(f"/api/accounts/{account_id}")

    assert partial.status_code == 200
    assert partial.json()["name"] == "Original Account Updated"
    assert partial.json()["positioning"] == payload["positioning"]
    assert partial.json()["hotspot_sources"] == payload["hotspot_sources"]
    assert after_partial.status_code == 200
    assert after_partial.json()["name"] == "Original Account Updated"


def test_patch_account_multiple_fields_update():
    payload = {
        "name": "Multi Update Account",
        "positioning": "Original positioning.",
        "topic_scoring_prompt": "Score topics from 0 to 100.",
        "content_creation_prompt": "Write a concise title and body.",
        "hotspot_sources": ["weibo"],
    }

    with TestClient(app) as client:
        created = client.post("/api/accounts", json=payload)
        assert created.status_code == 201
        account_id = created.json()["id"]
        multi = client.patch(
            f"/api/accounts/{account_id}",
            json={
                "positioning": "Updated positioning.",
                "hotspot_sources": ["xiaohongshu", "weibo"],
            },
        )

    assert multi.status_code == 200
    assert multi.json()["positioning"] == "Updated positioning."
    assert multi.json()["hotspot_sources"] == ["xiaohongshu", "weibo"]
    assert multi.json()["name"] == payload["name"]


def test_patch_nonexistent_account_returns_404():
    with TestClient(app) as client:
        missing = client.patch(
            "/api/accounts/does-not-exist",
            json={"name": "Ghost Account"},
        )

    assert missing.status_code == 404


def test_account_updated_at_is_touched_by_orm_update_event():
    old_updated_at = datetime(2020, 1, 1, tzinfo=UTC)

    with Session(get_engine()) as session:
        session.execute(
            text("UPDATE account SET updated_at = :updated_at WHERE id = 'default-agent'"),
            {"updated_at": old_updated_at},
        )
        session.commit()

        account = session.get(Account, "default-agent")
        assert account is not None
        assert account.updated_at == old_updated_at

        account.positioning = "Updated without calling touch_updated_at explicitly."
        session.add(account)
        session.commit()
        session.refresh(account)
        updated_at = account.updated_at

    assert updated_at > old_updated_at


def test_account_hotspot_sources_are_stored_as_jsonb():
    with Session(get_engine()) as session:
        source_type = session.execute(
            text("SELECT pg_typeof(hotspot_sources)::text FROM account WHERE id = 'default-agent'")
        ).scalar_one()

    assert source_type == "jsonb"


def test_account_delete_rejects_referenced_account():
    with Session(get_engine()) as session:
        chat = ChatSession(
            account_id="default-agent",
            tenant_id="local",
            owner_user_id="local-user",
        )
        session.add(chat)
        session.commit()

    with TestClient(app) as client:
        deleted = client.delete("/api/accounts/default-agent")
        assert deleted.status_code == 409


def test_account_rejects_empty_or_unknown_hotspot_sources():
    payload = {
        "name": "Invalid Account",
        "positioning": "Account for validation tests.",
        "topic_scoring_prompt": "Test scoring prompt",
        "content_creation_prompt": "Test creation prompt",
        "hotspot_sources": [],
    }

    with TestClient(app) as client:
        empty_sources = client.post("/api/accounts", json=payload)
        assert empty_sources.status_code == 422

        unknown_sources = client.post(
            "/api/accounts",
            json={**payload, "hotspot_sources": ["unknown"]},
        )
        assert unknown_sources.status_code == 422


def test_auth_requires_bearer_token_when_enabled():
    with TestClient(auth_test_app()) as client:
        response = client.get("/api/accounts")

    assert response.status_code == 401


def test_auth_filters_accounts_by_allowed_account_ids():
    with Session(get_engine()) as session:
        session.add(
            Account(
                id="other-agent",
                name="Other Account",
                positioning="Other account",
                topic_scoring_prompt="Test",
                content_creation_prompt="Test",
                hotspot_sources=["weibo"],
            )
        )
        session.commit()

    with TestClient(auth_test_app()) as client:
        listed = client.get(
            "/api/accounts",
            headers=auth_headers(tenant_id="local", accounts=["default-agent"]),
        )
        forbidden = client.get(
            "/api/accounts/other-agent",
            headers=auth_headers(tenant_id="local", accounts=["default-agent"]),
        )

    assert listed.status_code == 200
    assert [account["id"] for account in listed.json()] == ["default-agent"]
    assert forbidden.status_code == 403


def test_auth_blocks_cross_tenant_chat_access():
    with TestClient(auth_test_app()) as client:
        created = client.post(
            "/api/chat/sessions",
            headers=auth_headers(tenant_id="local"),
            json={"account_id": "default-agent"},
        )
        assert created.status_code == 200
        session_id = created.json()["session_id"]

        blocked = client.get(
            f"/api/chat/sessions/{session_id}",
            headers=auth_headers(tenant_id="tenant_b"),
        )

    assert blocked.status_code == 404


def test_run_routes_are_not_public_api():
    with TestClient(app) as client:
        created = client.post("/api/chat/runs", json={})
        fetched = client.get("/api/chat/runs/run_missing")
        events = client.get("/api/chat/runs/run_missing/events")

    assert created.status_code == 404
    assert fetched.status_code == 404
    assert events.status_code == 404


def test_chat_run_completes_with_plain_reply():
    model = FakeModel([AIMessage(content="plain reply")])
    install_fake_model(model)

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"account_id": "default-agent"}).json()
        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages/stream",
            json={
                "message": "plain message",
            },
        )
        assert response.status_code == 200
        payload = wait_for_terminal_session(client, session["session_id"])

    assert payload["latest_execution"]["status"] == "completed"
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant"]
    assert payload["messages"][-1]["content"] == "plain reply"


def test_chat_message_route_runs_in_background():
    install_fake_model(FakeModel([AIMessage(content="background reply")]))

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"account_id": "default-agent"}).json()
        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages",
            json={
                "message": "background message",
            },
        )
        assert response.status_code == 200
        assert response.json()["status"] == "running"
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
        session = client.post("/api/chat/sessions", json={"account_id": "default-agent"}).json()
        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages/stream",
            json={"message": "please remember this"},
        )
        assert response.status_code == 200
        payload = wait_for_terminal_session(client, session["session_id"])

    assert any(
        item["content"] == "stable memory"
        for item in payload["memory"]["long_term_memories"]
    )


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
        session = client.post("/api/chat/sessions", json={"account_id": "default-agent"}).json()
        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages/stream",
            json={"message": "temporary preference"},
        )
        assert response.status_code == 200
        payload = wait_for_terminal_session(client, session["session_id"])

    assert payload["memory"]["long_term_memories"] == []


def test_chat_session_title_is_generated_by_model():
    generated_title = "Deep finance title that is too long"
    install_fake_model(FakeModel([AIMessage(content="plain reply")]), title=generated_title)

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"account_id": "default-agent"}).json()
        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages/stream",
            json={
                "message": "Please generate a short title for this session",
            },
        )
        assert response.status_code == 200
        wait_for_terminal_session(client, session["session_id"])
        expected_title = generated_title.replace(" ", "")[:15]
        sessions = wait_for_session_title(client, expected_title=expected_title)

    assert sessions[0]["title"] != "New Session"
    assert sessions[0]["title"] == expected_title
    assert len(sessions[0]["title"]) <= 15


def test_chat_session_title_falls_back_when_model_fails():
    message = "Long fallback title message for testing title generation"
    install_fake_model(
        FakeModel([AIMessage(content="plain reply")]),
        error=RuntimeError("title failed"),
    )

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"account_id": "default-agent"}).json()
        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages/stream",
            json={
                "message": message,
            },
        )
        assert response.status_code == 200
        wait_for_terminal_session(client, session["session_id"])
        sessions = client.get("/api/chat/sessions").json()

    assert sessions[0]["title"] == message.replace(" ", "")[:15]
    assert len(sessions[0]["title"]) <= 15


def test_message_stream_returns_runtime_events():
    install_fake_model(FakeModel([AIMessage(content="streamed reply")]))

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions", json={"account_id": "default-agent"}).json()
        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages/stream",
            json={
                "message": "stream this reply",
            },
        )

    assert response.status_code == 200
    assert "event: token" in response.text
    assert "\"type\":\"token\"" in response.text


def test_interrupted_run_can_be_resumed():
    with Session(get_engine()) as session:
        chat = ChatSession(
            account_id="default-agent",
            tenant_id="local",
            owner_user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            session_id=chat.id,
            account_id="default-agent",
            tenant_id="local",
            created_by_user_id="local-user",
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
        invocation.user_message_id = message.id
        execution = AgentExecution(invocation_id=invocation.id, status=RunStatus.interrupted)
        session.add(invocation)
        session.add(execution)
        session.commit()
        execution_id = execution.id
        session_id = chat.id

    install_fake_model(FakeModel([AIMessage(content="resumed reply")]))

    with TestClient(app) as client:
        response = client.post(
            f"/api/chat/sessions/{session_id}/resume/stream",
            json={"message": "continue"},
        )

    assert response.status_code == 200
    assert "event: token" in response.text
    assert "\"name\":\"execution_completed\"" in response.text
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        assert execution.status == RunStatus.completed

def test_chat_session_can_be_deleted_with_related_rows():
    build_checkpointer()
    build_store()
    with Session(get_engine()) as session:
        chat = ChatSession(
            title="Deletable session",
            account_id="default-agent",
            tenant_id="local",
            owner_user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            session_id=chat.id,
            account_id="default-agent",
            tenant_id="local",
            created_by_user_id="local-user",
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
        session.add(
            ChatMessage(
                session_id=chat.id,
                role=MessageRole.system,
                content="legacy unbound session message",
            )
        )
        session.flush()
        invocation.user_message_id = message.id
        execution = AgentExecution(invocation_id=invocation.id, status=RunStatus.completed)
        session.add(invocation)
        session.add(execution)
        session.add(
            MemoryRecord(
                tenant_id=chat.tenant_id,
                user_id=chat.owner_user_id,
                account_id=chat.account_id,
                session_id=chat.id,
                memory_scope=MemoryScope.short_term,
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
        session.execute(
            text(
                """
                INSERT INTO store (prefix, key, value)
                VALUES (:prefix, 'summary', '{"content":"stale"}'::jsonb)
                ON CONFLICT (prefix, key) DO UPDATE SET value = EXCLUDED.value
                """
            ),
            {"prefix": f"sessions.{chat.langgraph_thread_id}"},
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
        assert (
            session.get(AgentInvocation, invocation_id)
            is None
        )
        assert (
            session.get(AgentExecution, execution_id)
            is None
        )
        assert (
            session.exec(
                select(MemoryRecord).where(
                    MemoryRecord.session_id == session_id,
                    MemoryRecord.memory_scope == MemoryScope.short_term,
                )
            ).first()
            is None
        )
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
        assert (
            session.execute(
                text("SELECT 1 FROM store WHERE prefix = :prefix"),
                {"prefix": f"sessions.{thread_id}"},
            ).first()
            is None
        )


def test_chat_session_delete_keeps_rows_when_persistence_cleanup_fails(monkeypatch):
    with Session(get_engine()) as session:
        chat = ChatSession(
            title="cleanup failure",
            account_id="default-agent",
            tenant_id="local",
            owner_user_id="local-user",
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
            account_id="default-agent",
            tenant_id="local",
            owner_user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            session_id=chat.id,
            account_id="default-agent",
            tenant_id="local",
            created_by_user_id="local-user",
        )
        session.add(invocation)
        session.flush()
        session.add(AgentExecution(invocation_id=invocation.id, status=RunStatus.running))
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
        session = client.post("/api/chat/sessions", json={"account_id": "default-agent"}).json()
        response = client.post(
            f"/api/chat/sessions/{session['session_id']}/messages/stream",
            json={
                "message": "please remember my preference",
            },
        )
        assert response.status_code == 200
        payload = wait_for_terminal_session(client, session["session_id"])

    assert payload["latest_execution"]["status"] == "completed"
    assert any(message["role"] == "tool" for message in payload["messages"])
    assert any(
        item["content"] == "prefers concise replies"
        for item in payload["memory"]["long_term_memories"]
    )


def test_running_run_can_be_cancel_requested():
    with Session(get_engine()) as session:
        chat = ChatSession(
            account_id="default-agent",
            tenant_id="local",
            owner_user_id="local-user",
        )
        session.add(chat)
        session.flush()
        invocation = AgentInvocation(
            session_id=chat.id,
            account_id="default-agent",
            tenant_id="local",
            created_by_user_id="local-user",
        )
        session.add(invocation)
        session.flush()
        execution = AgentExecution(invocation_id=invocation.id, status=RunStatus.running)
        session.add(execution)
        session.commit()
        session_id = chat.id

    with TestClient(app) as client:
        cancelled = client.post(f"/api/chat/sessions/{session_id}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["latest_execution"]["cancel_requested_at"] is not None

