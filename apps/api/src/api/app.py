import logging
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from uuid import uuid4

from agent.runtime.container import RuntimeContainer
from api.router import router
from core.client_ip import resolve_client_ip
from core.config import Env, Settings, get_settings
from core.logging import configure_logging
from core.rate_limit import RateLimitRule, RateLimitUnavailable, RedisRateLimiter
from db.session import close_database, get_engine, init_database
from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from services.admin_service import AdminService
from services.agent_service import AgentService
from services.auth_service import AuthService
from services.catalog_service import CatalogService
from services.conversation_service import ConversationService, ExecutionDispatcher
from services.model_configuration_service import ModelConfigurationService
from sqlmodel import Session

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
    auth_service = AuthService(app.state.settings)
    with Session(get_engine(app.state.settings)) as session:
        auth_service.bootstrap_default_admin(session)

    runtime = getattr(app.state, "runtime", None)
    agent_service = AgentService(app.state.settings, runtime=runtime)
    starter = getattr(agent_service, "start", None)
    if callable(starter):
        starter()
    app.state.agent_service = agent_service
    app.state.catalog_service = CatalogService()
    dispatcher_factory = getattr(app.state, "execution_dispatcher_factory", None)
    execution_dispatcher = (
        dispatcher_factory(agent_service) if dispatcher_factory is not None else None
    )
    conversation_service = ConversationService(
        agent_service,
        execution_dispatcher=execution_dispatcher,
    )
    app.state.conversation_service = conversation_service
    app.state.auth_service = auth_service
    app.state.admin_service = AdminService(auth_service)
    app.state.model_configuration_service = ModelConfigurationService(app.state.settings)
    app.state.rate_limiter = RedisRateLimiter(app.state.settings)


def _shutdown(app: FastAPI) -> None:
    app.state.ready = False
    for closer in (
        getattr(getattr(app.state, "conversation_service", None), "close", None),
        getattr(getattr(app.state, "agent_service", None), "close", None),
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
        init_database(app.state.settings)
        _register_services(app)
        app.state.ready = True
        logger.info("Application started.")
    except Exception:
        logger.exception("Application startup failed.")
        app.state.ready = False
        raise

    try:
        yield
    finally:
        _shutdown(app)


def create_app(
    settings: Settings | None = None,
    runtime: RuntimeContainer | None = None,
    execution_dispatcher_factory: Callable[[AgentService], ExecutionDispatcher] | None = None,
) -> FastAPI:
    settings = settings or get_settings()
    configure_logging("api", settings)

    app = FastAPI(
        title=settings.server.app_name,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.runtime = runtime
    app.state.execution_dispatcher_factory = execution_dispatcher_factory

    @app.exception_handler(RequestValidationError)
    async def redact_sensitive_validation_errors(
        request: Request, exc: RequestValidationError
    ) -> JSONResponse:
        if not _is_model_config_path(request.url.path):
            return await request_validation_exception_handler(request, exc)
        errors = []
        for raw_error in exc.errors():
            error = dict(raw_error)
            error["input"] = "[REDACTED]"
            if "ctx" in error:
                error["ctx"] = "[REDACTED]"
            errors.append(error)
        return JSONResponse(status_code=422, content={"detail": jsonable_encoder(errors)})

    app.add_middleware(
        CORSMiddleware,
        allow_origins=_normalize_origins(settings.server.frontend_origins),
        allow_origin_regex=(
            r"^https?://(localhost|127\.0\.0\.1):\d+$" if settings.env == Env.development else None
        ),
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(router)

    @app.middleware("http")
    async def request_context(request: Request, call_next) -> Response:
        request_id = str(uuid4())
        request.state.request_id = request_id
        rule = _rate_limit_rule(request.method, request.url.path)
        if rule is not None and app.state.settings.env != Env.test:
            identity = resolve_client_ip(request, app.state.settings)
            try:
                app.state.rate_limiter.check(request.url.path, identity, rule)
            except PermissionError as exc:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=429,
                    content={"detail": str(exc), "request_id": request_id},
                )
            except RateLimitUnavailable as exc:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=503,
                    content={"detail": str(exc), "request_id": request_id},
                )
        response = await call_next(request)
        if _is_model_config_path(request.url.path):
            response.headers["Cache-Control"] = "no-store"
        response.headers["X-Request-ID"] = request_id
        return response

    return app


def _rate_limit_rule(method: str, path: str) -> RateLimitRule | None:
    if method.upper() != "POST":
        return None
    if path in {
        "/api/auth/login",
        "/api/auth/register",
    }:
        return RateLimitRule(limit=10, window_seconds=60)
    return None


def _is_model_config_path(path: str) -> bool:
    return path == "/api/admin/model-config" or path.startswith(
        "/api/admin/model-config/"
    )


app = create_app()
