from collections.abc import Iterable
from typing import Any

from agent.graph.factory import build_agent_graph
from agent.runtime.checkpoint import build_checkpointer, build_store
from agent.runtime.executor import agent_executor
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage
from main import app


class FakeModel:
    def __init__(self, responses: Iterable[AIMessage]) -> None:
        self.responses = iter(responses)
        self.calls: list[list[Any]] = []

    def invoke(self, messages: list[Any]) -> AIMessage:
        self.calls.append(messages)
        return next(self.responses)


def install_fake_model(model: FakeModel) -> None:
    agent_executor.model = model
    agent_executor.checkpointer = build_checkpointer()
    agent_executor.store = build_store()
    agent_executor.graph = build_agent_graph(
        model=model,
        tools=agent_executor.tools,
        checkpointer=agent_executor.checkpointer,
        store=agent_executor.store,
    )


def test_account_crud_contract():
    payload = {
        "id": "test-agent",
        "name": "测试 Agent",
        "description": "用于测试账号配置。",
        "instructions": "回复保持简洁。",
    }

    with TestClient(app) as client:
        created = client.post("/api/accounts", json=payload)
        assert created.status_code == 201
        assert created.json()["instructions"] == payload["instructions"]

        updated = client.put(
            "/api/accounts/test-agent",
            json={"name": "测试 Agent 2", "instructions": "回复更直接。"},
        )
        assert updated.status_code == 200
        assert updated.json()["name"] == "测试 Agent 2"
        assert updated.json()["instructions"] == "回复更直接。"

        deleted = client.delete("/api/accounts/test-agent")
        assert deleted.status_code == 204
        assert client.get("/api/accounts/test-agent").status_code == 404


def test_chat_run_completes_with_plain_reply():
    install_fake_model(FakeModel([AIMessage(content="plain reply")]))

    with TestClient(app) as client:
        session = client.post("/api/chat/sessions").json()
        response = client.post(
            "/api/chat/runs",
            json={
                "session_id": session["session_id"],
                "account_id": "default-agent",
                "message": "你好",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        payload = client.get(f"/api/chat/runs/{run_id}").json()

    assert payload["status"] == "completed"
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
                            "args": {"content": "偏好：回答要短句", "kind": "preference"},
                            "type": "tool_call",
                        }
                    ],
                ),
                AIMessage(content="已记住。"),
            ]
        )
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/chat/runs",
            json={
                "account_id": "default-agent",
                "message": "请记住我偏好短句。",
            },
        )
        assert response.status_code == 200
        run_id = response.json()["run_id"]
        payload = client.get(f"/api/chat/runs/{run_id}").json()

    assert payload["status"] == "completed"
    assert any(message["role"] == "tool" for message in payload["messages"])
    assert any(
        item["content"] == "偏好：回答要短句"
        for item in payload["memory"]["long_term_memories"]
    )
