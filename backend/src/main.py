import logging
from collections.abc import Iterable
from contextlib import asynccontextmanager

from api.router import router
from core.config import Env, Settings, get_settings
from db.session import close_db, init_db
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from services.catalog_service import CatalogService

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
    app.state.catalog_service = CatalogService()


def _startup(app: FastAPI) -> None:
    init_db()
    app.state.ready = True
    logger.info("Application startup completed.")


def _shutdown(app: FastAPI) -> None:
    app.state.ready = False
    for closer in (
        getattr(app.state.catalog_service, "close", None),
        close_db,
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
        title=settings.app_name,
        lifespan=lifespan,
    )
    _register_services(app)

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_normalize_origins(settings.frontend_origins),
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
