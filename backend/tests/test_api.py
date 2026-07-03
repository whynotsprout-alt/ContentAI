import time
from collections.abc import Iterable
from typing import Any

from agent.graph.factory import build_agent_graph
from agent.runtime.checkpoint import (
    build_checkpointer,
    build_store,
    close_runtime_persistence,
)
from agent.runtime.executor import agent_executor
from db.session import engine
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from main import app
from models.chat import AgentRun, ChatSession
from models.enums import RunStatus
from sqlmodel import Session


class FakeModel:
    def __init__(self, responses: Iterable[AIMessage]) -> None:
        self.responses = iter(responses)
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(messages)
        return next(self.responses)


def install_fake_model(model: FakeModel) -> None:
    close_runtime_persistence()
    agent_executor.model = model
    agent_executor.checkpointer = build_checkpointer()
    agent_executor.store = build_store()
    agent_executor.graph = build_agent_graph(
        model=model,
        tools=agent_executor.tools,
        checkpointer=agent_executor.checkpointer,
        store=agent_executor.store,
    )


def wait_for_terminal_run(client: TestClient, run_id: str) -> dict[str, Any]:
    deadline = time.monotonic() + 10
    payload: dict[str, Any] = {}
    while time.monotonic() < deadline:
        response = client.get(f"/api/chat/runs/{run_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in {"completed", "failed", "cancelled", "interrupted"}:
            return payload
        time.sleep(0.1)
    raise AssertionError(f"run did not finish in time: {payload}")


def test_account_crud_contract():
    payload = {
        "name": "测试账号",
        "positioning": "用于测试的 AI 内容账号。",
        "topic_scoring_prompt": "给出可执行打分规则，输出 0-100。",
        "content_creation_prompt": "输出标题和正文，限制口吻并符合账号定位。",
        "hotspot_sources": ["douyin", "weibo"],
    }

    with TestClient(app) as client:
        created = client.post("/api/accounts", json=payload)
        assert created.status_code == 201
        account_id = created.json()["id"]
        assert created.json()["positioning"] == payload["positioning"]
        assert created.json()["hotspot_sources"] == payload["hotspot_sources"]

        updated = client.put(
            f"/api/accounts/{account_id}",
            json={
                "name": "测试账号 2",
                "hotspot_sources": ["xiaohongshu"],
            },
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "测试账号 2"
        assert updated.json()["hotspot_sources"] == ["xiaohongshu"]

        deleted = client.delete(f"/api/accounts/{account_id}")
        assert deleted.status_code == 204
        assert client.get(f"/api/accounts/{account_id}").status_code == 404


def test_account_delete_rejects_referenced_account():
    with Session(engine) as session:
        chat = ChatSession()
        session.add(chat)
        session.flush()
        session.add(
            AgentRun(
                session_id=chat.id,
                account_id="default-agent",
                user_message="只创建引用",
                status=RunStatus.completed,
            )
        )
        session.commit()

    with TestClient(app) as client:
        deleted = client.delete("/api/accounts/default-agent")
        assert deleted.status_code == 409


def test_account_rejects_empty_or_unknown_hotspot_sources():
    payload = {
        "name": "错误账号",
        "positioning": "用于回归的账号。",
        "topic_scoring_prompt": "测试评分规则",
        "content_creation_prompt": "测试生成规则",
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


def test_chat_run_completes_with_plain_reply():
    model = FakeModel([AIMessage(content="plain reply")])
    install_fake_model(model)

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions").json()
        response = client.post(
            "/api/chat/runs",
            json={
                "session_id": session["session_id"],
                "account_id": "default-agent",
                "message": "测试消息",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        payload = wait_for_terminal_run(client, run_id)

    assert payload["status"] == "completed"
    assert payload["attempt_count"] >= 1
    assert [message["role"] for message in payload["messages"]] == ["user", "assistant"]
    assert payload["messages"][-1]["content"] == "plain reply"


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
                                "content": "用户偏好：希望回复简洁。",
                                "kind": "preference",
                            },
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="处理完成"),
            ]
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/chat/runs",
            json={
                "account_id": "default-agent",
                "message": "请记住我的偏好。",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        payload = wait_for_terminal_run(client, run_id)

    assert payload["status"] == "completed"
    assert any(message["role"] == "tool" for message in payload["messages"])
    assert any(
        item["content"] == "用户偏好：希望回复简洁。"
        for item in payload["memory"]["long_term_memories"]
    )


def test_queued_run_can_be_cancelled():
    install_fake_model(FakeModel([AIMessage(content="will not run")]))

    with TestClient(app) as client:
        response = client.post(
            "/api/chat/runs",
            json={"account_id": "default-agent", "message": "取消这次运行"},
        )
        assert response.status_code == 200
        cancelled = client.post(f"/api/chat/runs/{response.json()['run_id']}/cancel")

    assert cancelled.status_code == 200
    assert cancelled.json()["cancel_requested_at"] is not None
