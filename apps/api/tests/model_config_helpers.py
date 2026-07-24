DEFAULT_MODEL_CONFIG_ID = "default-model-config"
TEST_MODEL_CONFIG_API_KEY = "test-only-model-config-api-key-7890"

DEFAULT_MODEL_RUNTIME_PARAMETERS: dict[str, int | float] = {
    "temperature": 0.2,
    "context_window_tokens": 32_000,
    "chat_max_tokens": 8_000,
    "structured_max_tokens": 8_000,
}


def model_runtime_parameters(**overrides: int | float) -> dict[str, int | float]:
    return {**DEFAULT_MODEL_RUNTIME_PARAMETERS, **overrides}
