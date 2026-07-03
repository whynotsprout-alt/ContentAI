from db.session import engine
from models.account import Account
from models.base import json_dumps
from models.chat import AgentRun
from models.memory import MemoryRecord
from models.schemas import AccountCreate, AccountDetail, AccountSummary, AccountUpdate
from services.errors import (
    AccountInUseError,
    AccountNotFoundError,
    AccountValidationError,
)
from sqlmodel import Session, col, select


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
            if not payload.name:
                raise AccountValidationError("Account name is required")
            account = Account(
                name=payload.name,
                positioning=payload.positioning,
                topic_scoring_prompt=payload.topic_scoring_prompt,
                content_creation_prompt=payload.content_creation_prompt,
                hotspot_sources=payload.hotspot_sources,
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
            if "positioning" in updates:
                account.positioning = updates["positioning"]
            if "topic_scoring_prompt" in updates:
                account.topic_scoring_prompt = updates["topic_scoring_prompt"]
            if "content_creation_prompt" in updates:
                account.content_creation_prompt = updates["content_creation_prompt"]
            if "hotspot_sources" in updates:
                account.hotspot_sources = updates["hotspot_sources"]

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
            if self._account_has_references(session, account_id):
                raise AccountInUseError(
                    "Account has chat history or memories and cannot be deleted"
                )
            session.delete(account)
            session.commit()

    @staticmethod
    def _account_has_references(session: Session, account_id: str) -> bool:
        has_runs = session.exec(
            select(AgentRun.id).where(AgentRun.account_id == account_id).limit(1)
        ).first()
        namespace_prefix = json_dumps(["accounts", account_id])
        has_memories = session.exec(
            select(MemoryRecord.id)
            .where(col(MemoryRecord.namespace).startswith(namespace_prefix[:-1]))
            .limit(1)
        ).first()
        return has_runs is not None or has_memories is not None

    @staticmethod
    def _account_to_summary(account: Account) -> AccountSummary:
        return AccountSummary(
            id=account.id,
            name=account.name,
            positioning=account.positioning,
            topic_scoring_prompt=account.topic_scoring_prompt,
            content_creation_prompt=account.content_creation_prompt,
            hotspot_sources=account.hotspot_sources,
        )

    def _account_to_detail(self, account: Account) -> AccountDetail:
        return AccountDetail(**self._account_to_summary(account).model_dump())


catalog_service = CatalogService()
