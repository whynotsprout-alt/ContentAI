from __future__ import annotations

import inspect
from types import SimpleNamespace
from typing import Any

import contentai.agent.runtime.execution_services as execution_services_module
from auth_helpers import default_test_auth_context
from client import ApiClient
from contentai.agent.context.window import TokenCounter
from contentai.agent.runtime.container import RuntimeContainer
from contentai.agent.runtime.execution_services import AgentPostExecutionService
from contentai.api.app import create_app
from contentai.core.config import get_settings
from contentai.core.model_config_crypto import ModelConfigurationSecretProtector
from contentai.core.security import authenticate_request
from contentai.db.session import get_engine
from contentai.models.chat import (
    AgentExecution,
    AgentInvocation,
    ChatMessage,
    ChatSession,
    ExecutionOutbox,
)
from contentai.models.enums import MessageRole, RunStatus
from contentai.models.model_configuration import ModelConfiguration
from contentai.services import tasks as tasks_module
from langchain_core.messages import AIMessage
from model_config_helpers import DEFAULT_MODEL_CONFIG_ID, model_runtime_parameters
from sqlalchemy import text
from sqlmodel import Session, select


class _UnusedGateway:
    def build_agent_model(self, *, tools: list[Any] | None = None) -> Any:
        raise AssertionError("no execution model should be built by these API tests")


def _test_app():
    runtime = RuntimeContainer(
        settings=get_settings(),
        model_gateway=_UnusedGateway(),
        checkpointer=object(),
    )
    app = create_app(settings=get_settings(), runtime=runtime)
    app.dependency_overrides[authenticate_request] = default_test_auth_context
    return app


def _seed_chat(session_id: str) -> None:
    with Session(get_engine()) as session:
        session.add(
            ChatSession(
                id=session_id,
                agent_id="default-agent",
                agent_version_id="default-agent-v1",
                user_id="local-user",
            )
        )
        session.commit()


def _post_message(client: ApiClient, session_id: str, message: str):
    client.app.state.rate_limiter = SimpleNamespace(check=lambda *_args, **_kwargs: None)
    return client.post(
        f"/api/chat/sessions/{session_id}/messages",
        json={"message": message},
    )


def test_message_without_active_model_configuration_is_503_and_writes_nothing() -> None:
    _seed_chat("session-no-model")
    with Session(get_engine()) as session:
        active = session.get(ModelConfiguration, DEFAULT_MODEL_CONFIG_ID)
        assert active is not None
        session.delete(active)
        session.commit()

    with ApiClient(_test_app()) as client:
        response = _post_message(client, "session-no-model", "must not be persisted")

    assert response.status_code == 503
    assert response.json()["detail"]["code"] == "MODEL_NOT_CONFIGURED"
    with Session(get_engine()) as session:
        assert session.exec(select(ChatMessage)).all() == []
        assert session.exec(select(AgentInvocation)).all() == []
        assert session.exec(select(AgentExecution)).all() == []
        assert session.exec(select(ExecutionOutbox)).all() == []


def test_new_executions_snapshot_active_configuration_version() -> None:
    _seed_chat("session-model-v1")
    _seed_chat("session-model-v2")
    app = _test_app()
    with ApiClient(app) as client:
        first = _post_message(client, "session-model-v1", "first model")
        assert first.status_code == 202

        with Session(get_engine()) as session:
            current = session.get(ModelConfiguration, DEFAULT_MODEL_CONFIG_ID)
            assert current is not None
            current.is_active = False
            current.superseded_at = current.created_at
            session.add(current)
            session.flush()
            protector = ModelConfigurationSecretProtector(
                get_settings().model_config_encryption_key
            )
            session.add(
                ModelConfiguration(
                    id="model-config-v2",
                    version=2,
                    base_url="https://models-v2.test.invalid/openai",
                    model_name="model-v2",
                    api_key_ciphertext=protector.encrypt("second-test-key"),
                    api_key_fingerprint=protector.fingerprint("second-test-key"),
                    api_key_hint=protector.hint("second-test-key"),
                    is_active=True,
                    created_by_user_id="local-user",
                )
            )
            session.commit()

        second = _post_message(client, "session-model-v2", "second model")
        assert second.status_code == 202

    with Session(get_engine()) as session:
        first_execution = session.get(AgentExecution, first.json()["execution_id"])
        second_execution = session.get(AgentExecution, second.json()["execution_id"])
        assert first_execution is not None
        assert second_execution is not None
        assert first_execution.model_config_id == DEFAULT_MODEL_CONFIG_ID
        assert second_execution.model_config_id == "model-config-v2"
        assert first_execution.model_config_id != second_execution.model_config_id


def test_runtime_resolves_inactive_execution_snapshot_after_active_switch() -> None:
    protector = ModelConfigurationSecretProtector(get_settings().model_config_encryption_key)
    with Session(get_engine()) as session:
        first = session.get(ModelConfiguration, DEFAULT_MODEL_CONFIG_ID)
        assert first is not None
        first.is_active = False
        session.add(first)
        session.flush()
        session.add(
            ModelConfiguration(
                id="model-config-active-v2",
                version=2,
                base_url="https://active-v2.test.invalid/root",
                model_name="active-v2-model",
                **model_runtime_parameters(
                    temperature=0.7,
                    context_window_tokens=200_000,
                    chat_max_tokens=12_000,
                    structured_max_tokens=6_000,
                ),
                api_key_ciphertext=protector.encrypt("active-v2-key"),
                api_key_fingerprint=protector.fingerprint("active-v2-key"),
                api_key_hint=protector.hint("active-v2-key"),
                is_active=True,
                created_by_user_id="local-user",
            )
        )
        session.commit()

    container = RuntimeContainer(settings=get_settings(), checkpointer=object())
    old_gateway = container.gateway_for_model_config(DEFAULT_MODEL_CONFIG_ID)
    new_gateway = container.gateway_for_model_config("model-config-active-v2")

    assert old_gateway.model_config_id == DEFAULT_MODEL_CONFIG_ID
    assert old_gateway.model_name == "test-model"
    assert old_gateway.base_url == "https://models.test.invalid/v1"
    assert old_gateway.temperature == 0.2
    assert old_gateway.context_window_tokens == 32_000
    assert old_gateway.chat_max_tokens == 8_000
    assert old_gateway.structured_max_tokens == 8_000
    assert new_gateway.model_config_id == "model-config-active-v2"
    assert new_gateway.model_name == "active-v2-model"
    assert new_gateway.base_url == "https://active-v2.test.invalid/root"
    assert new_gateway.temperature == 0.7
    assert new_gateway.context_window_tokens == 200_000
    assert new_gateway.chat_max_tokens == 12_000
    assert new_gateway.structured_max_tokens == 6_000
    assert old_gateway is container.gateway_for_model_config(DEFAULT_MODEL_CONFIG_ID)
    assert old_gateway is not new_gateway
    assert "active-v2-key" not in repr(new_gateway)


def test_background_postprocessing_uses_execution_snapshot(
    monkeypatch,
) -> None:
    _seed_chat("session-background-old-config")
    app = _test_app()
    with ApiClient(app) as client:
        created = _post_message(
            client,
            "session-background-old-config",
            "background must retain old config",
        )
        assert created.status_code == 202
    execution_id = created.json()["execution_id"]
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.status = RunStatus.completed
        session.add(execution)
        session.add(
            ChatMessage(
                session_id=execution.session_id,
                invocation_id=execution.invocation_id,
                execution_id=execution.id,
                role=MessageRole.assistant,
                content="completed response",
            )
        )
        session.add(
            ExecutionOutbox(
                execution_id=execution.id,
                model_config_id=execution.model_config_id,
                kind="postprocess",
            )
        )
        active = session.get(ModelConfiguration, DEFAULT_MODEL_CONFIG_ID)
        assert active is not None
        active.is_active = False
        session.add(active)
        session.flush()
        protector = ModelConfigurationSecretProtector(
            get_settings().model_config_encryption_key
        )
        session.add(
            ModelConfiguration(
                id="model-config-background-v2",
                version=2,
                base_url="https://background-v2.test.invalid/root",
                model_name="background-v2-model",
                api_key_ciphertext=protector.encrypt("background-v2-key"),
                api_key_fingerprint=protector.fingerprint("background-v2-key"),
                api_key_hint=protector.hint("background-v2-key"),
                is_active=True,
                created_by_user_id="local-user",
            )
        )
        session.commit()

    selected_ids: list[str] = []
    selected_gateway = object()
    container = RuntimeContainer(
        settings=get_settings(),
        model_gateway=selected_gateway,
        checkpointer=object(),
    )

    def select_gateway(model_config_id: str):
        selected_ids.append(model_config_id)
        return selected_gateway

    memory_gateways: list[object] = []
    title_gateways: list[object] = []
    monkeypatch.setattr(container, "gateway_for_model_config", select_gateway)
    monkeypatch.setattr(
        execution_services_module,
        "_extract_memory_background",
        lambda **kwargs: memory_gateways.append(kwargs["model_gateway"]),
    )
    monkeypatch.setattr(
        execution_services_module,
        "_refresh_short_summary_background",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        execution_services_module,
        "_update_title_background",
        lambda **kwargs: title_gateways.append(kwargs["model_gateway"]),
    )

    AgentPostExecutionService(
        container=container,
        settings=get_settings(),
    ).process(
        execution_id=execution_id,
        model_config_id=DEFAULT_MODEL_CONFIG_ID,
    )

    assert selected_ids == [DEFAULT_MODEL_CONFIG_ID]
    assert memory_gateways == [selected_gateway]
    assert title_gateways == [selected_gateway]


def test_postprocess_consumer_requires_delivery_model_configuration_id() -> None:
    parameter = inspect.signature(AgentPostExecutionService.process).parameters[
        "model_config_id"
    ]

    assert parameter.default is inspect.Parameter.empty


def test_queued_worker_resume_retry_keep_snapshot_after_active_switch(
    monkeypatch,
) -> None:
    class RecordingModel:
        def __init__(self, responses: list[AIMessage]) -> None:
            self.responses = iter(responses)
            self.calls = 0

        def invoke(self, _messages: list[Any]) -> AIMessage:
            self.calls += 1
            return next(self.responses)

    class RecordingGateway:
        def __init__(self, responses: list[AIMessage]) -> None:
            self.model = RecordingModel(responses)
            self.token_count_calls = 0

        def build_agent_model(self, *, tools: list[Any] | None = None) -> RecordingModel:
            return self.model

        def build_hotspot_filter_model(self) -> object:
            return object()

        def build_token_counter(self, *, tools: list[Any] | None = None) -> TokenCounter:
            self.token_count_calls += 1
            return TokenCounter(provider_count=lambda _messages: 1, tools=tools)

        def build_research_final_model(self) -> object:
            return object()

        def close(self) -> bool:
            return True

    old_gateway = RecordingGateway(
        [
            AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "remember",
                        "args": {"content": "retain old execution configuration"},
                        "id": "call-model-config-resume",
                        "type": "tool_call",
                    }
                ],
            ),
            AIMessage(content="old configuration resumed"),
            AIMessage(content="old configuration retried"),
        ]
    )
    new_gateway = RecordingGateway([AIMessage(content="new configuration")])
    selected_ids: list[str] = []
    container = RuntimeContainer(settings=get_settings())

    def build_gateway(**kwargs: Any):
        model_config_id = str(kwargs["model_config_id"])
        selected_ids.append(model_config_id)
        return {
            DEFAULT_MODEL_CONFIG_ID: old_gateway,
            "model-config-worker-v2": new_gateway,
        }[model_config_id]

    monkeypatch.setattr("contentai.agent.runtime.container.ModelGateway", build_gateway)
    app = create_app(settings=get_settings(), runtime=container)
    app.dependency_overrides[authenticate_request] = default_test_auth_context
    _seed_chat("session-worker-old-config")
    _seed_chat("session-worker-new-config")

    class NoopHeartbeat:
        def __init__(self, *_args: Any, **_kwargs: Any) -> None:
            pass

        def start(self) -> None:
            pass

        def close(self) -> None:
            pass

    monkeypatch.setattr(tasks_module, "WorkerHeartbeat", NoopHeartbeat)
    monkeypatch.setattr(tasks_module, "_agent_service", lambda: app.state.agent_service)

    with ApiClient(app) as client:
        queued = _post_message(
            client,
            "session-worker-old-config",
            "queue against the old model configuration",
        )
        assert queued.status_code == 202
        execution_id = queued.json()["execution_id"]

        protector = ModelConfigurationSecretProtector(
            get_settings().model_config_encryption_key
        )
        with Session(get_engine()) as session:
            old_configuration = session.get(ModelConfiguration, DEFAULT_MODEL_CONFIG_ID)
            assert old_configuration is not None
            old_configuration.is_active = False
            session.add(old_configuration)
            session.add(
                ModelConfiguration(
                    id="model-config-worker-v2",
                    version=2,
                    base_url="https://worker-v2.test.invalid/root",
                    model_name="worker-v2-model",
                    api_key_ciphertext=protector.encrypt("worker-v2-key"),
                    api_key_fingerprint=protector.fingerprint("worker-v2-key"),
                    api_key_hint=protector.hint("worker-v2-key"),
                    is_active=True,
                    created_by_user_id="local-user",
                )
            )
            session.commit()

        tasks_module.execute_agent.run(
            execution_id=execution_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
        )
        with Session(get_engine()) as session:
            execution = session.get(AgentExecution, execution_id)
            assert execution is not None
            assert execution.status == RunStatus.waiting_input
        detail = client.get("/api/chat/sessions/session-worker-old-config")
        interrupt_id = detail.json()["latest_execution"]["interrupt"]["interrupt_id"]

        resumed = client.post(
            f"/api/chat/runs/{execution_id}/resume",
            json={"interrupt_id": interrupt_id, "decision": "approve"},
        )
        assert resumed.status_code == 200
        tasks_module.execute_agent.run(
            execution_id=execution_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
        )

        with Session(get_engine()) as session:
            execution = session.get(AgentExecution, execution_id)
            assert execution is not None
            assert execution.status == RunStatus.completed
            execution.status = RunStatus.pending
            execution.finished_at = None
            execution.claimed_at = None
            execution.heartbeat_at = None
            execution.lease_expires_at = None
            execution.worker_id = None
            session.add(execution)
            session.commit()

        tasks_module.execute_agent.run(
            execution_id=execution_id,
            model_config_id=DEFAULT_MODEL_CONFIG_ID,
        )
        new_turn = _post_message(
            client,
            "session-worker-new-config",
            "queue against the new model configuration",
        )
        assert new_turn.status_code == 202

    with Session(get_engine()) as session:
        old_execution = session.get(AgentExecution, execution_id)
        new_execution = session.get(AgentExecution, new_turn.json()["execution_id"])
        assert old_execution is not None
        assert new_execution is not None
        assert old_execution.status == RunStatus.completed
        assert old_execution.model_config_id == DEFAULT_MODEL_CONFIG_ID
        assert new_execution.model_config_id == "model-config-worker-v2"
    assert old_gateway.model.calls == 3
    assert new_gateway.model.calls == 0
    assert old_gateway.token_count_calls == 1
    assert new_gateway.token_count_calls == 1
    assert DEFAULT_MODEL_CONFIG_ID in selected_ids
    assert "model-config-worker-v2" in selected_ids


def test_postprocess_rejects_delivery_and_outbox_configuration_mismatches() -> None:
    _seed_chat("session-postprocess-config-fence")
    app = _test_app()
    with ApiClient(app) as client:
        created = _post_message(
            client,
            "session-postprocess-config-fence",
            "postprocess must enforce the model configuration fence",
        )
        assert created.status_code == 202
    execution_id = created.json()["execution_id"]
    protector = ModelConfigurationSecretProtector(get_settings().model_config_encryption_key)
    with Session(get_engine()) as session:
        execution = session.get(AgentExecution, execution_id)
        assert execution is not None
        execution.status = RunStatus.completed
        session.add(execution)
        session.add(
            ExecutionOutbox(
                id="outbox-postprocess-config-fence",
                execution_id=execution.id,
                model_config_id=execution.model_config_id,
                kind="postprocess",
            )
        )
        session.add(
            ModelConfiguration(
                id="model-config-postprocess-v2",
                version=2,
                base_url="https://postprocess-v2.test.invalid/root",
                model_name="postprocess-v2-model",
                api_key_ciphertext=protector.encrypt("postprocess-v2-key"),
                api_key_fingerprint=protector.fingerprint("postprocess-v2-key"),
                api_key_hint=protector.hint("postprocess-v2-key"),
                is_active=False,
                created_by_user_id="local-user",
            )
        )
        session.commit()

    gateway_calls: list[str] = []
    container = RuntimeContainer(
        settings=get_settings(),
        model_gateway=object(),
        checkpointer=object(),
    )
    container.gateway_for_model_config = (  # type: ignore[method-assign]
        lambda model_config_id: gateway_calls.append(model_config_id)
    )
    service = AgentPostExecutionService(container=container, settings=get_settings())

    service.process(
        execution_id=execution_id,
        model_config_id="model-config-postprocess-v2",
    )
    with Session(get_engine()) as session:
        outbox = session.get(ExecutionOutbox, "outbox-postprocess-config-fence")
        assert outbox is not None
        assert outbox.status == "pending"
        assert outbox.processing_attempts == 0
        session.exec(text("SET session_replication_role = replica"))
        session.exec(
            text(
                "UPDATE executionoutbox SET model_config_id = :model_config_id "
                "WHERE id = :outbox_id"
            ),
            params={
                "model_config_id": "model-config-postprocess-v2",
                "outbox_id": outbox.id,
            },
        )
        session.exec(text("SET session_replication_role = origin"))
        session.commit()

    service.process(
        execution_id=execution_id,
        model_config_id=DEFAULT_MODEL_CONFIG_ID,
    )
    with Session(get_engine()) as session:
        outbox = session.get(ExecutionOutbox, "outbox-postprocess-config-fence")
        assert outbox is not None
        assert outbox.status == "pending"
        assert outbox.processing_attempts == 0
    assert gateway_calls == []
