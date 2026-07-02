from typing import Annotated

from db.session import get_session
from fastapi import Depends, Request
from services.catalog_service import CatalogService
from sqlmodel import Session

SessionDep = Annotated[Session, Depends(get_session)]


def get_catalog_service(request: Request) -> CatalogService:
    return request.app.state.catalog_service


CatalogServiceDep = Annotated[CatalogService, Depends(get_catalog_service)]
