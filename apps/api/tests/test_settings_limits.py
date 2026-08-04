from contentai.core.config import Settings
from contentai.core.config.llm import DEFAULT_CONTEXT_WINDOW_TOKENS, DEFAULT_MAX_OUTPUT_TOKENS


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
        database={
            "url": "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
        },
        auth={"mode": "disabled"},
    )

    assert not hasattr(settings.auth, "mode")
