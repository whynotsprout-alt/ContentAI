from typing import Annotated

from api.dependencies import CatalogServiceDep
from fastapi import APIRouter, HTTPException, Path
from models.schemas import AccountCreate, AccountDetail, AccountSummary, AccountUpdate
from services.errors import (
    AccountInUseError,
    AccountNotFoundError,
    AccountValidationError,
)

router = APIRouter()

AccountIdPath = Annotated[
    str,
    Path(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$"),
]


@router.get("/accounts", response_model=list[AccountSummary])
def list_accounts(service: CatalogServiceDep) -> list[AccountSummary]:
    return service.list_accounts()


@router.get("/accounts/{account_id}", response_model=AccountDetail)
def get_account(account_id: AccountIdPath, service: CatalogServiceDep) -> AccountDetail:
    try:
        return service.get_account(account_id)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc


@router.post("/accounts", response_model=AccountDetail, status_code=201)
def create_account(payload: AccountCreate, service: CatalogServiceDep) -> AccountDetail:
    try:
        return service.create_account(payload)
    except AccountValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.put("/accounts/{account_id}", response_model=AccountDetail)
@router.patch("/accounts/{account_id}", response_model=AccountDetail)
def update_account(
    account_id: AccountIdPath,
    payload: AccountUpdate,
    service: CatalogServiceDep,
) -> AccountDetail:
    try:
        return service.update_account(account_id, payload)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc
    except AccountValidationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.delete("/accounts/{account_id}", status_code=204)
def delete_account(account_id: AccountIdPath, service: CatalogServiceDep) -> None:
    try:
        service.delete_account(account_id)
    except AccountNotFoundError as exc:
        raise HTTPException(status_code=404, detail="Account not found") from exc
    except AccountInUseError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
