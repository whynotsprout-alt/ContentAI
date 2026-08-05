from __future__ import annotations

import base64
import hashlib
from decimal import Decimal
from importlib import import_module
from types import SimpleNamespace

import pytest
from contentai.core.config import Settings, get_settings
from contentai.db.session import build_engine, get_engine
from model_config_helpers import (
    DEFAULT_MODEL_CONFIG_ID,
    TEST_MODEL_CONFIG_API_KEY,
    model_runtime_parameters,
    resolve_test_database_url,
)
from pydantic import ValidationError
from sqlalchemy import create_engine, func, inspect, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import IntegrityError, StatementError
from sqlalchemy.pool import QueuePool
from sqlmodel import Session, select


def _fernet_key(seed: int) -> str:
    return base64.urlsafe_b64encode(bytes([seed]) * 32).decode("ascii")


def _crypto_module():
    try:
        return import_module("contentai.core.model_config_crypto")
    except ModuleNotFoundError:
        pytest.fail("model configuration secret protection module is missing")


def _model_class():
    try:
        return import_module("contentai.models.model_configuration").ModelConfiguration
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


def test_test_database_url_is_isolated_per_xdist_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "CONTENTAI_TEST_DATABASE_URL",
        "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test_shared",
    )
    monkeypatch.setenv("PYTEST_XDIST_WORKER", "gw3")

    assert make_url(resolve_test_database_url()).database == "contentai_test_shared_gw3"


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


def test_update_api_mode_defaults_preserve_existing_configuration() -> None:
    service_module = import_module("contentai.services.model_configuration_service")

    assert (
        service_module.ModelConfigurationService._resolve_api_mode(
            SimpleNamespace(api_mode="responses"), None
        )
        == "responses"
    )
    assert (
        service_module.ModelConfigurationService._resolve_api_mode(None, None)
        == "chat_completions"
    )
    assert (
        service_module.ModelConfigurationService._resolve_api_mode(None, "responses")
        == "responses"
    )


def test_update_without_api_mode_probes_and_persists_the_active_mode() -> None:
    service_module = import_module("contentai.services.model_configuration_service")
    key = _fernet_key(5)
    protector = _crypto_module().ModelConfigurationSecretProtector(key)
    plaintext_key = "existing-provider-key"
    active = SimpleNamespace(
        version=7,
        base_url="https://models.example.test/v1",
        api_mode="responses",
        api_key_ciphertext=protector.encrypt(plaintext_key),
        api_key_fingerprint=protector.fingerprint(plaintext_key),
        api_key_hint=protector.hint(plaintext_key),
        input_price_per_million_usd=Decimal("5.000000"),
        output_price_per_million_usd=Decimal("25.000000"),
    )

    class Repository:
        def __init__(self) -> None:
            self.saved: dict[str, object] = {}

        def get_active(self, _session):
            return active

        def replace_active(self, _session, **kwargs):
            self.saved = kwargs
            return SimpleNamespace(**kwargs)

    class Prober:
        def __init__(self) -> None:
            self.api_modes: list[str] = []

        def probe(self, base_url, api_key, model_name, *, api_mode):
            assert api_key == plaintext_key
            assert model_name == "selected-model"
            self.api_modes.append(api_mode)
            return SimpleNamespace(base_url=base_url, model_validated=True)

    repository = Repository()
    prober = Prober()
    service = service_module.ModelConfigurationService(
        _development_settings(**{"CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY": key}),
        repository=repository,
        prober=prober,
    )
    session = SimpleNamespace(in_transaction=lambda: None)

    service.update(
        session,
        actor_user_id="local-user",
        request_id="request-mode-compat",
        base_url="https://models.example.test/v1",
        api_key=None,
        model_name="selected-model",
        **model_runtime_parameters(),
        expected_version=7,
    )

    assert prober.api_modes == ["responses"]
    assert repository.saved["api_mode"] == "responses"


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
    monkeypatch.delenv("CONTENTAI_MODEL_CONFIGURATION__ENCRYPTION_KEY", raising=False)

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
        database={"url": resolve_test_database_url()},
        **{"CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY": _fernet_key(4)},
    )

    assert settings.model_config_encryption_key.get_secret_value() == _fernet_key(4)


def test_undeclared_model_configuration_environment_key_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv(
        "CONTENTAI_MODEL_CONFIGURATION__ENCRYPTION_KEY",
        _fernet_key(5),
    )

    with pytest.raises(ValidationError, match="CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY"):
        _development_settings()


def test_single_underscore_model_config_environment_key_is_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY", raising=False)
    monkeypatch.setenv("CONTENTAI_MODEL_CONFIG_ENCRYPTION_KEY", _fernet_key(5))

    with pytest.raises(ValidationError, match="CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY"):
        _development_settings()


def test_only_declared_model_config_environment_key_controls_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY", "malformed-test-value")
    monkeypatch.setenv(
        "CONTENTAI_MODEL_CONFIGURATION__ENCRYPTION_KEY",
        _fernet_key(6),
    )
    with pytest.raises(ValidationError, match="CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY"):
        _development_settings()

    monkeypatch.setenv("CONTENTAI_MODEL_CONFIG__ENCRYPTION_KEY", _fernet_key(6))
    monkeypatch.setenv(
        "CONTENTAI_MODEL_CONFIGURATION__ENCRYPTION_KEY",
        "malformed-test-value",
    )
    settings = _development_settings()
    assert settings.model_config_encryption_key.get_secret_value() == _fernet_key(6)


def test_default_active_model_configuration_fixture_uses_consistent_secret_material() -> None:
    crypto = _crypto_module()
    with Session(get_engine()) as session:
        configuration = session.get(_model_class(), DEFAULT_MODEL_CONFIG_ID)

    assert configuration is not None
    protector = crypto.ModelConfigurationSecretProtector(
        get_settings().model_config_encryption_key
    )
    assert protector.decrypt(configuration.api_key_ciphertext) == TEST_MODEL_CONFIG_API_KEY
    assert configuration.api_key_fingerprint == protector.fingerprint(TEST_MODEL_CONFIG_API_KEY)
    assert configuration.api_key_hint == protector.hint(TEST_MODEL_CONFIG_API_KEY)


def test_application_engine_hides_bound_parameters_in_database_errors() -> None:
    engine = build_engine(get_settings())
    try:
        assert engine.hide_parameters is True
    finally:
        engine.dispose()


def test_model_configuration_persistence_failure_rolls_back_and_hides_parameters() -> None:
    service_module = import_module("contentai.services.model_configuration_service")
    ciphertext_marker = "gAAAA-test-ciphertext-must-not-escape"

    class Repository:
        @staticmethod
        def get_active(_session):
            return None

        @staticmethod
        def replace_active(_session, **_kwargs):
            raise StatementError(
                "write failed",
                "INSERT INTO modelconfiguration VALUES (?)",
                (ciphertext_marker,),
                RuntimeError("database unavailable"),
            )

    class Prober:
        @staticmethod
        def probe(base_url, _api_key, _model_name):
            return SimpleNamespace(base_url=base_url, model_validated=True)

    session = SimpleNamespace(rollback_calls=0)
    session.rollback = lambda: setattr(session, "rollback_calls", session.rollback_calls + 1)
    service = service_module.ModelConfigurationService(
        get_settings(),
        repository=Repository(),
        prober=Prober(),
    )

    with pytest.raises(service_module.ModelConfigurationPersistenceFailed) as exc_info:
        service.update(
            session,
            actor_user_id="local-user",
            request_id="request-test",
            base_url="https://models.example.test/v1",
            api_key="test-only-key",
            model_name="test-model",
            **model_runtime_parameters(),
            expected_version=0,
        )

    assert session.rollback_calls == 1
    assert ciphertext_marker not in str(exc_info.value)
    assert ciphertext_marker not in repr(exc_info.value)
    assert exc_info.value.__cause__ is None


def test_model_configuration_probe_uses_an_isolated_preflight_transaction() -> None:
    service_module = import_module("contentai.services.model_configuration_service")

    class Repository:
        def __init__(self) -> None:
            self.read_sessions: list[Session] = []

        def get_active(self, read_session):
            self.read_sessions.append(read_session)
            read_session.exec(text("SELECT 1")).one()
            return None

    class Prober:
        def __init__(self, engine, caller_session, pending) -> None:
            self.engine = engine
            self.caller_session = caller_session
            self.pending = pending

        def probe(self, base_url, _api_key, _model_name):
            assert self.engine.pool.checkedout() == 0
            assert self.pending in self.caller_session.new
            return SimpleNamespace(base_url=base_url, model_validated=True)

    engine = create_engine("sqlite+pysqlite://", poolclass=QueuePool)
    repository = Repository()
    try:
        with Session(engine) as caller_session:
            pending = _model_class()(
                version=99,
                base_url="https://pending.example.test/v1",
                model_name="pending-model",
                api_key_ciphertext="pending-ciphertext",
                api_key_fingerprint="f" * 64,
                api_key_hint="...test",
                created_by_user_id="pending-user",
            )
            caller_session.add(pending)
            service = service_module.ModelConfigurationService(
                get_settings(),
                repository=repository,
                prober=Prober(engine, caller_session, pending),
            )

            result = service.probe(
                caller_session,
                base_url="https://models.example.test/v1",
                api_key="test-only-key",
                model_name="test-model",
            )

            assert result.model_validated is True
            assert repository.read_sessions[0] is not caller_session
            assert pending in caller_session.new
            assert caller_session.in_transaction() is not None
    finally:
        engine.dispose()


def test_update_probe_failure_preserves_caller_pending_writes() -> None:
    service_module = import_module("contentai.services.model_configuration_service")
    network_module = import_module("contentai.services.model_config_network")

    class Repository:
        @staticmethod
        def get_active(read_session):
            read_session.exec(text("SELECT 1")).one()
            return None

    class FailingProbe:
        def __init__(self, engine, caller_session, pending) -> None:
            self.engine = engine
            self.caller_session = caller_session
            self.pending = pending

        def probe(self, _base_url, _api_key, _model_name):
            assert self.engine.pool.checkedout() == 0
            assert self.pending in self.caller_session.new
            raise network_module.ModelProbeFailed("expected test failure")

    engine = create_engine("sqlite+pysqlite://", poolclass=QueuePool)
    try:
        with Session(engine) as caller_session:
            pending = _model_class()(
                version=99,
                base_url="https://pending.example.test/v1",
                model_name="pending-model",
                api_key_ciphertext="pending-ciphertext",
                api_key_fingerprint="f" * 64,
                api_key_hint="...test",
                created_by_user_id="pending-user",
            )
            caller_session.add(pending)
            service = service_module.ModelConfigurationService(
                get_settings(),
                repository=Repository(),
                prober=FailingProbe(engine, caller_session, pending),
            )

            with pytest.raises(network_module.ModelProbeFailed):
                service.update(
                    caller_session,
                    actor_user_id="local-user",
                    request_id="request-test",
                    base_url="https://models.example.test/v1",
                    api_key="test-only-key",
                    model_name="test-model",
                    **model_runtime_parameters(),
                    expected_version=0,
                )

            assert pending in caller_session.new
            assert caller_session.in_transaction() is not None
    finally:
        engine.dispose()


def test_legacy_probe_fails_closed_for_responses_mode() -> None:
    service_module = import_module("contentai.services.model_configuration_service")
    network_module = import_module("contentai.services.model_config_network")

    class Repository:
        @staticmethod
        def get_active(_session):
            return None

    class LegacyProbe:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str, str | None]] = []

        def probe(self, base_url, api_key, model_name):
            self.calls.append((base_url, api_key, model_name))
            return SimpleNamespace(base_url=base_url, model_validated=True)

    prober = LegacyProbe()
    service = service_module.ModelConfigurationService(
        get_settings(), repository=Repository(), prober=prober
    )
    session = SimpleNamespace()

    chat_result = service.probe(
        session,
        base_url="https://models.example.test/v1",
        api_key="test-only-key",
        model_name="test-model",
        api_mode="chat_completions",
    )
    with pytest.raises(network_module.ModelProbeFailed):
        service.probe(
            session,
            base_url="https://models.example.test/v1",
            api_key="test-only-key",
            model_name="test-model",
            api_mode="responses",
        )

    assert chat_result.model_validated is True
    assert prober.calls == [
        ("https://models.example.test/v1", "test-only-key", "test-model")
    ]


@pytest.mark.parametrize("signature_kind", ["variadic", "positional_only"])
def test_probe_requires_explicit_keyword_api_mode_for_responses(signature_kind: str) -> None:
    service_module = import_module("contentai.services.model_configuration_service")
    network_module = import_module("contentai.services.model_config_network")

    class Repository:
        @staticmethod
        def get_active(_session):
            return None

    calls: list[str] = []

    class VariadicProbe:
        @staticmethod
        def probe(base_url, _api_key, _model_name, **_ignored):
            calls.append(base_url)
            return SimpleNamespace(base_url=base_url, model_validated=True)

    class PositionalOnlyProbe:
        @staticmethod
        def probe(base_url, _api_key, _model_name, api_mode, /):
            calls.append(f"{base_url}:{api_mode}")
            return SimpleNamespace(base_url=base_url, model_validated=True)

    prober = VariadicProbe() if signature_kind == "variadic" else PositionalOnlyProbe()
    service = service_module.ModelConfigurationService(
        get_settings(), repository=Repository(), prober=prober
    )

    with pytest.raises(network_module.ModelProbeFailed):
        service.probe(
            SimpleNamespace(),
            base_url="https://models.example.test/v1",
            api_key="test-only-key",
            model_name="test-model",
            api_mode="responses",
        )

    assert calls == []


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
        "api_mode",
        "base_url",
        "model_name",
        "input_price_per_million_usd",
        "output_price_per_million_usd",
        "temperature",
        "context_window_tokens",
        "chat_max_tokens",
        "structured_max_tokens",
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
    assert not columns["input_price_per_million_usd"]["nullable"]
    assert not columns["output_price_per_million_usd"]["nullable"]
    assert columns["temperature"]["nullable"]
    assert not columns["context_window_tokens"]["nullable"]
    assert not columns["chat_max_tokens"]["nullable"]
    assert not columns["structured_max_tokens"]["nullable"]

    usage_columns = {column["name"]: column for column in inspector.get_columns("modelusage")}
    for name in ("input_cost_usd", "output_cost_usd", "total_cost_usd"):
        assert name in usage_columns
        assert not usage_columns[name]["nullable"]

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
    from contentai.models.chat import AgentExecution, AgentInvocation, ChatSession, ExecutionOutbox

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


def _constraint_test_configuration(**overrides: object):
    # These rows exercise database constraints, not secret decryption.
    values: dict[str, object] = {
        "version": 201,
        "base_url": "https://constraint.example.test/v1",
        "model_name": "constraint-model",
        "api_key_ciphertext": "constraint-test-ciphertext",
        "api_key_fingerprint": "c" * 64,
        "api_key_hint": "...test",
        "is_active": False,
        "created_by_user_id": "local-user",
    }
    values.update(overrides)
    return _model_class()(**values)


def test_postgres_allows_multiple_inactive_model_configurations() -> None:
    with Session(get_engine()) as session:
        session.add_all(
            [
                _constraint_test_configuration(id="inactive-one", version=201),
                _constraint_test_configuration(id="inactive-two", version=202),
            ]
        )
        session.flush()
        count = session.exec(
            select(func.count()).select_from(_model_class()).where(
                _model_class().is_active.is_(False)
            )
        ).one()
        assert count == 2
        session.rollback()


@pytest.mark.parametrize(
    ("overrides", "constraint_name"),
    [
        ({"id": "second-active", "version": 210, "is_active": True}, "active"),
        ({"id": "duplicate-version", "version": 1}, "version"),
        ({"id": "invalid-provider", "provider": "unsupported"}, "provider"),
        ({"id": "invalid-api-mode", "api_mode": "auto"}, "api_mode"),
        ({"id": "invalid-fingerprint", "api_key_fingerprint": "short"}, "fingerprint"),
        ({"id": "invalid-creator", "created_by_user_id": "missing-user"}, "creator"),
        ({"id": "temperature-low", "temperature": -0.01}, "temperature"),
        ({"id": "temperature-high", "temperature": 2.01}, "temperature"),
        ({"id": "context-zero", "context_window_tokens": 0}, "context"),
        ({"id": "chat-zero", "chat_max_tokens": 0}, "chat"),
        (
            {
                "id": "chat-exceeds-context",
                "context_window_tokens": 8_000,
                "chat_max_tokens": 8_000,
            },
            "chat",
        ),
        ({"id": "structured-zero", "structured_max_tokens": 0}, "structured"),
        (
            {
                "id": "structured-exceeds-context",
                "context_window_tokens": 8_000,
                "structured_max_tokens": 8_000,
            },
            "structured",
        ),
    ],
)
def test_postgres_rejects_invalid_model_configuration_rows_without_poisoning_session(
    overrides: dict[str, object],
    constraint_name: str,
) -> None:
    with Session(get_engine()) as session:
        with pytest.raises(IntegrityError):
            with session.begin_nested():
                session.add(_constraint_test_configuration(**overrides))
                session.flush()

        assert session.get(_model_class(), DEFAULT_MODEL_CONFIG_ID) is not None, constraint_name
