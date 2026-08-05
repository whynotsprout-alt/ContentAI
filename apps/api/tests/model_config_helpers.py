import os

from sqlalchemy.engine import make_url

DEFAULT_MODEL_CONFIG_ID = "default-model-config"
TEST_MODEL_CONFIG_API_KEY = "test-only-model-config-api-key-7890"
DEFAULT_TEST_DATABASE_URL = (
    "postgresql+psycopg://postgres:postgres@127.0.0.1:5432/contentai_test"
)

DEFAULT_MODEL_RUNTIME_PARAMETERS = {
    "temperature": 0.2,
    "context_window_tokens": 32_000,
    "chat_max_tokens": 8_000,
    "structured_max_tokens": 8_000,
}


def resolve_test_database_url() -> str:
    value = os.getenv("CONTENTAI_TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)
    worker_id = os.getenv("PYTEST_XDIST_WORKER", "").strip()
    if not worker_id or worker_id == "master":
        return value
    url = make_url(value)
    database = url.database
    if not database:
        return value
    suffix = f"_{worker_id}"
    worker_database = f"{database[: 63 - len(suffix)]}{suffix}"
    return url.set(database=worker_database).render_as_string(hide_password=False)


def model_runtime_parameters(**overrides: int | float | None) -> dict[str, int | float | None]:
    parameters = dict(DEFAULT_MODEL_RUNTIME_PARAMETERS)
    parameters.update(overrides)
    return parameters
