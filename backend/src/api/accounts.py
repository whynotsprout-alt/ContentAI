from typing import Annotated

from api.dependencies import AuthContextDep, CatalogServiceDep, SessionDep
from fastapi import APIRouter, HTTPException, Path
from models.schemas import AccountCreate, AccountDetail, AccountSummary, AccountUpdate
from services.errors import (
    AccountAlreadyExistsError,
    AccountInUseError,
    AccountNotFoundError,
    AccountPermissionError,
    AccountValidationError,
)

router = APIRouter()

AccountIdPath = Annotated[
    str,
    Path(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$"),
]


@router.get("/accounts", response_model=list[AccountSummary])
def list_accounts(
    service: CatalogServiceDep,
    session: SessionDep,
    auth: AuthContextDep,
) -> list[AccountSummary]:
    return service.list_accounts(session, auth)


@router.get("/accounts/{account_id}", response_model=AccountDetail)
def get_account(
    account_id: AccountIdPath,
    service: CatalogServiceDep,
    session: SessionDep,
    auth: AuthContextDep,
) -> AccountDetail:
    try:
        return service.get_account(session, account_id, auth)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc
    except AccountPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.post("/accounts", response_model=AccountDetail, status_code=201)
def create_account(
    payload: AccountCreate,
    service: CatalogServiceDep,
    session: SessionDep,
    auth: AuthContextDep,
) -> AccountDetail:
    try:
        return service.create_account(session, payload, auth)
    except AccountAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AccountValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AccountPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.patch(
    "/accounts/{account_id}",
    response_model=AccountDetail,
    summary="Update account (partial fields)",
)
def patch_account(
    account_id: AccountIdPath,
    payload: AccountUpdate,
    service: CatalogServiceDep,
    session: SessionDep,
    auth: AuthContextDep,
) -> AccountDetail:
    try:
        return service.update_account(session, account_id, payload, auth)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc
    except AccountAlreadyExistsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AccountValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except AccountPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(
    account_id: AccountIdPath,
    service: CatalogServiceDep,
    session: SessionDep,
    auth: AuthContextDep,
) -> None:
    try:
        service.delete_account(session, account_id, auth)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc
    except AccountInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except AccountPermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
