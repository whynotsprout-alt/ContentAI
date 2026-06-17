from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from articleforgeai.api.routes import router
from articleforgeai.core.config import get_settings
from articleforgeai.core.env_guard import assert_project_virtualenv
from articleforgeai.services.catalog import catalog_service
from articleforgeai.services.database import init_db
from articleforgeai.services.system_config import system_config_service


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    catalog_service.bootstrap_from_files_if_needed()
    system_config_service.bootstrap_if_needed()
    yield


def create_app() -> FastAPI:
    assert_project_virtualenv()
    settings = get_settings()
    app = FastAPI(title=settings.app_name, lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.frontend_origins,
        allow_origin_regex=r"http://(localhost|127\.0\.0\.1):\d+",
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.include_router(router)

    return app


app = create_app()
