import inspect
import json
import logging
from collections.abc import Callable, Iterable
from contextlib import asynccontextmanager
from typing import Any
from uuid import uuid4

from fastapi import FastAPI, Request, Response
from fastapi.encoders import jsonable_encoder
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import TypeAdapter
from sqlmodel import Session

from contentai.agent.runtime.container import RuntimeContainer
from contentai.api.router import router
from contentai.core.client_ip import resolve_client_ip
from contentai.core.config import Env, Settings, get_settings
from contentai.core.logging import configure_logging
from contentai.core.rate_limit import RateLimitRule, RateLimitUnavailable, RedisRateLimiter
from contentai.db.session import close_database, get_engine, init_database
from contentai.models.schemas import StreamEventV3
from contentai.services.admin_service import AdminService
from contentai.services.agent_service import AgentService
from contentai.services.auth_service import AuthService
from contentai.services.catalog_service import CatalogService
from contentai.services.conversation_service import ConversationService, ExecutionDispatcher
from contentai.services.event_stream import close_cached_event_streams
from contentai.services.model_configuration_service import ModelConfigurationService

logger = logging.getLogger(__name__)

SESSION_COOKIE_SECURITY_SCHEME = "SessionCookie"
CSRF_HEADER_SECURITY_SCHEME = "CsrfHeader"
_PUBLIC_API_OPERATIONS = frozenset(
    {
        ("get", "/api/health"),
        ("get", "/api/ready"),
        ("post", "/api/auth/register"),
        ("post", "/api/auth/login"),
    }
)
_STATE_CHANGING_METHODS = frozenset({"post", "put", "patch", "delete"})
_STREAM_EVENTS_PATH = "/api/chat/runs/{execution_id}/events"
_STREAM_EVENT_EXAMPLE: dict[str, Any] = {
    "schema_version": 3,
    "execution_id": "exe_example",
    "sequence": 1,
    "event_id": "exe_example:1",
    "channel": "lifecycle",
    "namespace": [],
    "attempt_id": "attempt_example",
    "message_id": None,
    "tool_call_id": None,
    "timestamp": "2026-08-04T00:00:00Z",
    "data": {"name": "execution_started", "status": "running"},
}
_STREAM_ERROR_EVENT_EXAMPLE: dict[str, Any] = {
    "schema_version": 3,
    "execution_id": "exe_example",
    "sequence": 2,
    "event_id": "exe_example:2",
    "channel": "errors",
    "namespace": [],
    "attempt_id": "attempt_example",
    "message_id": None,
    "tool_call_id": None,
    "timestamp": "2026-08-04T00:00:01Z",
    "data": {
        "name": "execution_failed",
        "code": "AGENT_EXECUTION_FAILED",
        "message": "The execution failed.",
        "retryable": False,
        "status": "failed",
    },
}
_STREAM_HEARTBEAT_EXAMPLE: dict[str, Any] = {"schema_version": 3}
_STREAM_CONTROL_EXAMPLE: dict[str, Any] = {
    "schema_version": 3,
    "execution_id": "exe_example",
    "channel": "errors",
    "data": {
        "name": "stream_exception",
        "code": "STREAM_EXCEPTION_ERROR",
        "message": "Event stream processing failed.",
    },
}
_STREAM_CONFLICT_CODES = (
    "INVALID_STREAM_CURSOR",
    "STREAM_REPLAY_GAP",
    "STREAM_REPLAY_EXPIRED",
    "STREAMING_DEGRADED",
)


def _openapi_sse_example(
    event: str,
    data: dict[str, Any],
    *,
    event_id: str | None = None,
) -> str:
    lines = [] if event_id is None else [f"id: {event_id}"]
    lines.extend(
        (
            f"event: {event}",
            f"data: {json.dumps(data, ensure_ascii=False, separators=(',', ':'))}",
            "",
        )
    )
    return "\n".join(lines) + "\n"


def _openapi_http_error_response(
    description: str,
    *,
    examples: dict[str, dict[str, Any]],
    detail_schema: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "description": description,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["detail"],
                    "properties": {"detail": detail_schema or {}},
                },
                "examples": examples,
            }
        },
    }


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
    app.state.catalog_service = CatalogService(app.state.settings)
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


def _install_openapi_contract(app: FastAPI) -> None:
    default_openapi = app.openapi

    def contract_openapi() -> dict[str, Any]:
        schema = default_openapi()
        components = schema.setdefault("components", {})
        security_schemes = components.setdefault("securitySchemes", {})
        security_schemes[SESSION_COOKIE_SECURITY_SCHEME] = {
            "type": "apiKey",
            "in": "cookie",
            "name": app.state.settings.auth.session_cookie_name,
            "description": "Opaque ContentAI login session cookie.",
        }
        security_schemes[CSRF_HEADER_SECURITY_SCHEME] = {
            "type": "apiKey",
            "in": "header",
            "name": "X-CSRF-Token",
            "description": "Double-submit CSRF token required for state-changing requests.",
        }

        for path, path_item in schema.get("paths", {}).items():
            if not path.startswith("/api/"):
                continue
            for method, operation in path_item.items():
                normalized_method = method.lower()
                if normalized_method not in {"get", "post", "put", "patch", "delete"}:
                    continue
                if (normalized_method, path) in _PUBLIC_API_OPERATIONS:
                    operation.pop("security", None)
                    continue
                requirement = {SESSION_COOKIE_SECURITY_SCHEME: []}
                if normalized_method in _STATE_CHANGING_METHODS:
                    # Both entries deliberately share one requirement object: OpenAPI
                    # interprets separate objects as alternatives (OR), not CSRF's AND.
                    requirement[CSRF_HEADER_SECURITY_SCHEME] = []
                operation["security"] = [requirement]

        schemas = components.setdefault("schemas", {})
        stream_event_schema = TypeAdapter(StreamEventV3).json_schema(
            ref_template="#/components/schemas/{model}"
        )
        stream_event_definitions = stream_event_schema.pop("$defs", {})
        schemas.update(stream_event_definitions)
        stream_event_schema["title"] = "StreamEventV3"
        stream_event_schema["examples"] = [
            _STREAM_EVENT_EXAMPLE,
            _STREAM_ERROR_EVENT_EXAMPLE,
        ]
        schemas["StreamEventV3"] = stream_event_schema

        event_operation = schema["paths"][_STREAM_EVENTS_PATH]["get"]
        event_responses = event_operation["responses"]
        event_responses["200"] = {
            "description": (
                "SSE data, heartbeat, and terminal transport-control frames. "
                "Only data frames advance Last-Event-ID."
            ),
            "content": {
                "text/event-stream": {
                    "schema": {"type": "string"},
                    "examples": {
                        "data": {
                            "summary": "Sequenced StreamEventV3 data frame",
                            "value": _openapi_sse_example(
                                "lifecycle",
                                _STREAM_EVENT_EXAMPLE,
                                event_id=_STREAM_EVENT_EXAMPLE["event_id"],
                            ),
                        },
                        "heartbeat": {
                            "summary": "Heartbeat control frame without an id",
                            "value": _openapi_sse_example(
                                "heartbeat",
                                _STREAM_HEARTBEAT_EXAMPLE,
                            ),
                        },
                        "control": {
                            "summary": "Terminal transport error without an id",
                            "value": _openapi_sse_example(
                                "errors",
                                _STREAM_CONTROL_EXAMPLE,
                            ),
                        },
                    },
                }
            },
        }
        event_responses["401"] = _openapi_http_error_response(
            "Authentication is required.",
            examples={
                "not_authenticated": {
                    "value": {"detail": "Not authenticated"},
                }
            },
        )
        event_responses["403"] = _openapi_http_error_response(
            "The authenticated account may not open this stream.",
            examples={
                "account_restricted": {
                    "value": {
                        "detail": {
                            "code": "USER_DISABLED",
                            "message": "Account is disabled",
                        }
                    },
                }
            },
        )
        event_responses["404"] = _openapi_http_error_response(
            "The execution does not exist in the caller's scope.",
            examples={
                "execution_not_found": {
                    "value": {
                        "detail": {
                            "code": "EXECUTION_NOT_FOUND",
                            "message": "Execution not found",
                        }
                    },
                }
            },
        )
        event_responses["409"] = _openapi_http_error_response(
            "The requested cursor cannot be replayed or streaming is degraded.",
            detail_schema={
                "type": "object",
                "required": ["code", "message"],
                "properties": {
                    "code": {"type": "string", "enum": list(_STREAM_CONFLICT_CODES)},
                    "message": {"type": "string"},
                },
            },
            examples={
                code.lower(): {
                    "value": {
                        "detail": {
                            "code": code,
                            "message": "The requested event stream cannot be opened.",
                        }
                    }
                }
                for code in _STREAM_CONFLICT_CODES
            },
        )
        default_validation_schema = {
            "$ref": "#/components/schemas/HTTPValidationError"
        }
        event_responses["422"] = {
            "description": (
                "FastAPI request validation failure or an invalid Last-Event-ID header."
            ),
            "content": {
                "application/json": {
                    "schema": {
                        "oneOf": [
                            default_validation_schema,
                            {
                                "type": "object",
                                "required": ["detail"],
                                "properties": {"detail": {"type": "string"}},
                            },
                        ]
                    },
                    "examples": {
                        "framework_validation": {
                            "summary": "Automatic FastAPI parameter validation",
                            "value": {
                                "detail": [
                                    {
                                        "type": "string_type",
                                        "loc": ["query", "after_sequence"],
                                        "msg": "Input should be a valid string",
                                        "input": None,
                                    }
                                ]
                            },
                        },
                        "after_sequence": {
                            "summary": "Manual fallback query validation",
                            "value": {
                                "detail": (
                                    "after_sequence must be a non-negative integer"
                                )
                            },
                        },
                        "last_event_id": {
                            "summary": "Custom Last-Event-ID validation",
                            "value": {
                                "detail": (
                                    "Last-Event-ID execution does not match request"
                                )
                            },
                        },
                    },
                }
            },
        }
        return schema

    app.openapi = contract_openapi


async def _shutdown(app: FastAPI) -> None:
    app.state.ready = False
    agent_service = getattr(app.state, "agent_service", None)
    agent_closer = getattr(agent_service, "aclose", None)
    if not callable(agent_closer):
        agent_closer = getattr(agent_service, "close", None)
    for closer in (
        getattr(getattr(app.state, "conversation_service", None), "close", None),
        agent_closer,
        close_cached_event_streams,
        close_database,
    ):
        if callable(closer):
            try:
                result = closer()
                if inspect.isawaitable(result):
                    await result
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
        await _shutdown(app)


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
    _install_openapi_contract(app)

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
                    headers={"X-Request-ID": request_id},
                )
            except RateLimitUnavailable as exc:
                from fastapi.responses import JSONResponse

                return JSONResponse(
                    status_code=503,
                    content={"detail": str(exc), "request_id": request_id},
                    headers={"X-Request-ID": request_id},
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
