import pytest
from contentai.core.config import Settings
from contentai.core.config.llm import DEFAULT_CONTEXT_WINDOW_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS
from model_config_helpers import resolve_test_database_url
from pydantic import ValidationError


def test_default_prompt_budget_and_recent_message_window():
    settings = Settings(
        _env_file=None,
        env="development",
        database={"url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai"},
    )

    assert settings.llm.context_window_tokens == DEFAULT_CONTEXT_WINDOW_TOKENS == 32_000
    assert settings.llm.chat_max_tokens == DEFAULT_MAX_OUTPUT_TOKENS == 8_000
    assert settings.llm.structured_max_tokens == DEFAULT_MAX_OUTPUT_TOKENS
    assert settings.agent.context_max_messages == 40


def test_retired_auth_modes_are_ignored():
    settings = Settings(
        _env_file=None,
        env="test",
        database={"url": resolve_test_database_url()},
        auth={"mode": "disabled"},
    )

    assert not hasattr(settings.auth, "mode")


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("temperature", -0.01, "CONTENTAI_LLM__TEMPERATURE"),
        ("temperature", 2.01, "CONTENTAI_LLM__TEMPERATURE"),
        ("temperature", float("nan"), "CONTENTAI_LLM__TEMPERATURE"),
        ("temperature", float("inf"), "CONTENTAI_LLM__TEMPERATURE"),
        ("context_window_tokens", 0, "CONTENTAI_LLM__CONTEXT_WINDOW_TOKENS"),
        ("chat_max_tokens", 0, "CONTENTAI_LLM__CHAT_MAX_TOKENS"),
        ("structured_max_tokens", 0, "CONTENTAI_LLM__STRUCTURED_MAX_TOKENS"),
    ],
)
def test_llm_runtime_limits_are_rejected_at_settings_boundary(
    field: str,
    value: int | float,
    message: str,
) -> None:
    with pytest.raises(ValidationError, match=message):
        Settings(
            _env_file=None,
            env="development",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai"
            },
            llm={field: value},
        )


@pytest.mark.parametrize("field", ["chat_max_tokens", "structured_max_tokens"])
def test_llm_output_budget_must_fit_context_window(field: str) -> None:
    with pytest.raises(ValidationError, match="must be less than"):
        Settings(
            _env_file=None,
            env="development",
            database={
                "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai"
            },
            llm={"context_window_tokens": 8, field: 8},
        )
