import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager

from agent.runtime.checkpoint import close_runtime_persistence
from agent.runtime.container import get_runtime_container, reset_runtime_container
from api.router import router
from core.config import Env, Settings, get_settings
from core.paths import ensure_runtime_dirs
from db.session import close_database, validate_database
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from services.catalog_service import CatalogService
from services.conversation_service import ConversationService

logger = logging.getLogger(__name__)


def _normalize_origins(frontend_origins: str | Iterable[str] | None) -> list[str]:
    if not frontend_origins:
        return []

    if isinstance(frontend_origins, str):
        frontend_origins = frontend_origins.split(",")

    normalized: list[str] = []
    seen: set[str] = set()
    for raw_origin in frontend_origins:
        if not raw_origin:
            continue
        for origin in str(raw_origin).split(","):
            trimmed = origin.strip()
            if trimmed and trimmed not in seen:
                normalized.append(trimmed)
                seen.add(trimmed)
    return normalized


def _register_services(app: FastAPI) -> None:
    runtime = get_runtime_container(app.state.settings)
    app.state.agent_runtime = runtime
    app.state.catalog_service = CatalogService()
    app.state.conversation_service = ConversationService(runtime)


def _startup(app: FastAPI) -> None:
    ensure_runtime_dirs()
    validate_database(app.state.settings)
    _register_services(app)
    app.state.ready = True
    logger.info("Application startup completed.")


def _shutdown_agent_runtime() -> None:
    close_runtime_persistence()
    reset_runtime_container()


def _shutdown(app: FastAPI) -> None:
    app.state.ready = False
    for closer in (
        getattr(getattr(app.state, "conversation_service", None), "close", None),
        _shutdown_agent_runtime,
        close_database,
    ):
        if callable(closer):
            try:
                closer()
            except Exception:
                logger.exception("Application shutdown hook failed: %s", closer)


@asynccontextmanager
async def lifespan(app: FastAPI):
    app.state.ready = False

    try:
        _startup(app)
    except Exception:
        logger.exception("Application startup failed.")
        app.state.ready = False
        raise

    try:
        yield
    finally:
        _shutdown(app)


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()

    app = FastAPI(
        title=settings.server.app_name,
        lifespan=lifespan,
    )
    app.state.settings = settings

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_normalize_origins(settings.server.frontend_origins),
        allow_origin_regex=(
            r"^https?://(localhost|127\.0\.0\.1):\d+$"
            if settings.env == Env.development
            else None
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    return app


app = create_app()
