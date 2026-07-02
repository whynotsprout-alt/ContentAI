from db.session import engine
from models.account import Account
from models.schemas import AccountCreate, AccountDetail, AccountSummary, AccountUpdate
from services.errors import (
    AccountAlreadyExistsError,
    AccountNotFoundError,
    AccountValidationError,
)
from sqlmodel import Session, select


class CatalogService:
    def list_accounts(self) -> list[AccountSummary]:
        with Session(engine) as session:
            rows = session.exec(select(Account).order_by(Account.id)).all()
            return [self._account_to_summary(account) for account in rows]

    def get_account(self, account_id: str) -> AccountDetail:
        with Session(engine) as session:
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFoundError(account_id)
            return self._account_to_detail(account)

    def create_account(self, payload: AccountCreate) -> AccountDetail:
        with Session(engine) as session:
            if session.get(Account, payload.id) is not None:
                raise AccountAlreadyExistsError(payload.id)
            if not payload.id or not payload.name:
                raise AccountValidationError("Account id and name are required")
            account = Account(
                id=payload.id,
                name=payload.name,
                description=payload.description,
                instructions=payload.instructions,
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return self._account_to_detail(account)

    def update_account(self, account_id: str, payload: AccountUpdate) -> AccountDetail:
        with Session(engine) as session:
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFoundError(account_id)

            updates = payload.model_dump(exclude_unset=True)
            if "name" in updates:
                if not updates["name"]:
                    raise AccountValidationError("Account name cannot be empty")
                account.name = updates["name"]
            if "description" in updates:
                account.description = updates["description"] or ""
            if "instructions" in updates:
                account.instructions = updates["instructions"] or ""

            account.touch_updated_at()
            session.add(account)
            session.commit()
            session.refresh(account)
            return self._account_to_detail(account)

    def delete_account(self, account_id: str) -> None:
        with Session(engine) as session:
            account = session.get(Account, account_id)
            if account is None:
                raise AccountNotFoundError(account_id)
            session.delete(account)
            session.commit()

    @staticmethod
    def _account_to_summary(account: Account) -> AccountSummary:
        return AccountSummary(
            id=account.id,
            name=account.name,
            description=account.description,
            instructions=account.instructions,
        )

    def _account_to_detail(self, account: Account) -> AccountDetail:
        return AccountDetail(**self._account_to_summary(account).model_dump())


catalog_service = CatalogService()
