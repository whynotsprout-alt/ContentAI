from __future__ import annotations

import base64
import hashlib
from importlib import import_module

import pytest
from core.config import Settings
from db.session import get_engine
from pydantic import ValidationError
from sqlalchemy import inspect, text
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session


def _fernet_key(seed: int) -> str:
    return base64.urlsafe_b64encode(bytes([seed]) * 32).decode("ascii")


def _crypto_module():
    try:
        return import_module("core.model_config_crypto")
    except ModuleNotFoundError:
        pytest.fail("model configuration secret protection module is missing")


def _model_class():
    try:
        return import_module("models.model_configuration").ModelConfiguration
    except ModuleNotFoundError:
        pytest.fail("model configuration ORM model is missing")


def _development_settings(**values: object) -> Settings:
    return Settings(
        _env_file=None,
        env="development",
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai"
        },
        **values,
    )


def test_fernet_roundtrip_and_deterministic_fingerprint() -> None:
    crypto = _crypto_module()
    protector = crypto.ModelConfigurationSecretProtector(_fernet_key(1))
    plaintext = "test-api-key-roundtrip"

    ciphertext = protector.encrypt(plaintext)

    assert ciphertext != plaintext
    assert protector.decrypt(ciphertext) == plaintext
    assert protector.fingerprint(plaintext) == hashlib.sha256(
        plaintext.encode("utf-8")
    ).hexdigest()
    assert protector.fingerprint(plaintext) == protector.fingerprint(plaintext)


def test_wrong_fernet_key_fails_closed_without_disclosing_secret() -> None:
    crypto = _crypto_module()
    plaintext = "test-api-key-wrong-key"
    ciphertext = crypto.ModelConfigurationSecretProtector(_fernet_key(2)).encrypt(plaintext)

    with pytest.raises(crypto.ModelConfigurationSecretError) as exc_info:
        crypto.ModelConfigurationSecretProtector(_fernet_key(3)).decrypt(ciphertext)

    error = str(exc_info.value)
    assert plaintext not in error
    assert ciphertext not in error


def test_safe_api_key_hint_only_reveals_a_small_suffix() -> None:
    crypto = _crypto_module()
    plaintext = "test-api-key-with-distinctive-prefix-and-suffix7890"

    hint = crypto.ModelConfigurationSecretProtector.hint(plaintext)

    assert hint.endswith("7890")
    assert plaintext not in hint
    assert "distinctive-prefix" not in hint
    assert len(hint) <= 8
    assert crypto.ModelConfigurationSecretProtector.hint("abc") == "..."


def test_development_settings_reject_missing_model_config_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY", raising=False)

    with pytest.raises(ValidationError, match="CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY"):
        _development_settings()


def test_settings_reject_malformed_model_config_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY", "malformed-test-value")

    with pytest.raises(ValidationError, match="CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY"):
        _development_settings()


def test_test_settings_accept_explicit_deterministic_model_config_key() -> None:
    settings = Settings(
        _env_file=None,
        env="test",
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
        },
        model_configuration={"encryption_key": _fernet_key(4)},
    )

    assert settings.model_configuration.encryption_key.get_secret_value() == _fernet_key(4)


def test_model_configuration_serialization_and_repr_exclude_secret_material() -> None:
    model_class = _model_class()
    ciphertext = "ciphertext-must-not-be-serialized"
    configuration = model_class(
        version=1,
        base_url="https://models.example.test/v1",
        model_name="test-model",
        api_key_ciphertext=ciphertext,
        api_key_fingerprint="a" * 64,
        api_key_hint="...7890",
        created_by_user_id="local-user",
    )

    assert "api_key_ciphertext" not in configuration.model_dump()
    assert ciphertext not in repr(configuration)


def test_model_configuration_metadata_and_execution_snapshot_contract() -> None:
    _model_class()
    inspector = inspect(get_engine())

    columns = {column["name"]: column for column in inspector.get_columns("modelconfiguration")}
    assert {
        "id",
        "version",
        "provider",
        "base_url",
        "model_name",
        "api_key_ciphertext",
        "api_key_fingerprint",
        "api_key_hint",
        "is_active",
        "validated_at",
        "created_at",
        "superseded_at",
        "created_by_user_id",
    } == set(columns)
    assert not columns["version"]["nullable"]
    assert not columns["api_key_ciphertext"]["nullable"]
    assert not columns["created_by_user_id"]["nullable"]

    execution_columns = {
        column["name"]: column for column in inspector.get_columns("agentexecution")
    }
    outbox_columns = {
        column["name"]: column for column in inspector.get_columns("executionoutbox")
    }
    assert not execution_columns["model_config_id"]["nullable"]
    assert not outbox_columns["model_config_id"]["nullable"]

    active_indexes = {
        index["name"]: index for index in inspector.get_indexes("modelconfiguration")
    }
    assert active_indexes["ux_modelconfiguration_active"]["unique"]
    assert "is_active" in str(
        active_indexes["ux_modelconfiguration_active"].get("dialect_options", {})
    )


def test_execution_outbox_model_configuration_mismatch_is_rejected() -> None:
    model_class = _model_class()
    from models.chat import AgentExecution, AgentInvocation, ChatSession, ExecutionOutbox

    with Session(get_engine()) as session:
        session.add_all(
            [
                model_class(
                    id="model-config-one",
                    version=101,
                    base_url="https://one.example.test/v1",
                    model_name="model-one",
                    api_key_ciphertext="ciphertext-one",
                    api_key_fingerprint="1" * 64,
                    api_key_hint="...one",
                    is_active=False,
                    created_by_user_id="local-user",
                ),
                model_class(
                    id="model-config-two",
                    version=102,
                    base_url="https://two.example.test/v1",
                    model_name="model-two",
                    api_key_ciphertext="ciphertext-two",
                    api_key_fingerprint="2" * 64,
                    api_key_hint="...two",
                    is_active=False,
                    created_by_user_id="local-user",
                ),
            ]
        )
        session.add(
            ChatSession(
                id="model-config-mismatch-session",
                agent_id="default-agent",
                agent_version_id="default-agent-v1",
                user_id="local-user",
            )
        )
        session.flush()
        session.add(
            AgentInvocation(
                id="model-config-mismatch-invocation",
                session_id="model-config-mismatch-session",
                agent_id="default-agent",
                user_id="local-user",
            )
        )
        session.flush()
        session.add(
            AgentExecution(
                id="model-config-mismatch-execution",
                invocation_id="model-config-mismatch-invocation",
                session_id="model-config-mismatch-session",
                agent_version_id="default-agent-v1",
                model_config_id="model-config-one",
            )
        )
        session.flush()
        session.add(
            ExecutionOutbox(
                execution_id="model-config-mismatch-execution",
                model_config_id="model-config-two",
            )
        )

        with pytest.raises(IntegrityError):
            session.flush()
        session.rollback()

    with get_engine().connect() as connection:
        assert (
            connection.execute(
                text(
                    "SELECT count(*) FROM executionoutbox "
                    "WHERE execution_id = 'model-config-mismatch-execution'"
                )
            ).scalar_one()
            == 0
        )
