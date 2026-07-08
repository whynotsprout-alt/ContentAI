from core.security import ACCOUNT_WILDCARD, AuthContext
from models.account import Account
from models.chat import AgentInvocation, ChatSession
from models.memory import MemoryRecord
from models.schemas import AccountCreate, AccountDetail, AccountSummary, AccountUpdate
from services.errors import (
    AccountAlreadyExistsError,
    AccountInUseError,
    AccountNotFoundError,
    AccountPermissionError,
    AccountValidationError,
)
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select


class CatalogService:
    def __init__(self) -> None:
        pass

    def list_accounts(self, session: Session, auth: AuthContext) -> list[AccountSummary]:
        query = select(Account).where(Account.tenant_id == auth.tenant_id).order_by(Account.id)
        if ACCOUNT_WILDCARD not in auth.allowed_account_ids:
            if not auth.allowed_account_ids:
                return []
            query = query.where(Account.id.in_(auth.allowed_account_ids))
        rows = session.exec(query).all()
        return [self._account_to_summary(account) for account in rows]

    def get_account(self, session: Session, account_id: str, auth: AuthContext) -> AccountDetail:
        account = self._get_account_for_auth(session, account_id, auth)
        if account is None:
            raise AccountNotFoundError(account_id)
        self._ensure_account_allowed(account_id, auth)
        return self._account_to_detail(account)

    def create_account(
        self,
        session: Session,
        payload: AccountCreate,
        auth: AuthContext,
    ) -> AccountDetail:
        self._ensure_account_admin(auth)
        tenant_id = self._tenant_id_for_auth(auth)
        self._ensure_name_available(session, tenant_id=tenant_id, name=payload.name)
        account = Account(
            tenant_id=tenant_id,
            name=payload.name,
            positioning=payload.positioning,
            topic_scoring_prompt=payload.topic_scoring_prompt,
            content_creation_prompt=payload.content_creation_prompt,
            hotspot_sources=payload.hotspot_sources,
        )
        session.add(account)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            self._raise_account_integrity_error(exc)
        session.refresh(account)
        return self._account_to_detail(account)

    def update_account(
        self,
        session: Session,
        account_id: str,
        payload: AccountUpdate,
        auth: AuthContext,
    ) -> AccountDetail:
        account = self._get_account_for_auth(session, account_id, auth)
        if account is None:
            raise AccountNotFoundError(account_id)
        self._ensure_account_allowed(account_id, auth)

        updates = payload.model_dump(exclude_unset=True)
        if "name" in updates:
            name = updates["name"]
            if name is None:
                raise AccountValidationError("Account name cannot be empty")
            self._ensure_name_available(
                session,
                tenant_id=account.tenant_id,
                name=name,
                exclude_account_id=account.id,
            )
            account.name = name
        if "positioning" in updates and updates["positioning"] is not None:
            account.positioning = updates["positioning"]
        if "topic_scoring_prompt" in updates and updates["topic_scoring_prompt"] is not None:
            account.topic_scoring_prompt = updates["topic_scoring_prompt"]
        if "content_creation_prompt" in updates and updates["content_creation_prompt"] is not None:
            account.content_creation_prompt = updates["content_creation_prompt"]
        if "hotspot_sources" in updates and updates["hotspot_sources"] is not None:
            account.hotspot_sources = updates["hotspot_sources"]

        session.add(account)
        try:
            session.commit()
        except IntegrityError as exc:
            session.rollback()
            self._raise_account_integrity_error(exc)
        session.refresh(account)
        return self._account_to_detail(account)

    def delete_account(
        self,
        session: Session,
        account_id: str,
        auth: AuthContext,
    ) -> None:
        account = self._get_account_for_auth(session, account_id, auth)
        if account is None:
            raise AccountNotFoundError(account_id)
        self._ensure_account_allowed(account_id, auth)
        if self._account_has_references(session, account_id):
            raise AccountInUseError(
                "Account has chat history or memories and cannot be deleted"
            )
        session.delete(account)
        session.commit()

    @staticmethod
    def _account_has_references(session: Session, account_id: str) -> bool:
        if (
            session.exec(
                select(ChatSession.id).where(ChatSession.account_id == account_id).limit(1)
            ).first()
            is not None
        ):
            return True

        if (
            session.exec(
                select(AgentInvocation.id)
                .where(AgentInvocation.account_id == account_id)
                .limit(1)
            ).first()
            is not None
        ):
            return True

        if (
            session.exec(
                select(MemoryRecord.id).where(MemoryRecord.account_id == account_id).limit(1)
            ).first()
            is not None
        ):
            return True

        return False

    @staticmethod
    def _get_account_for_auth(
        session: Session,
        account_id: str,
        auth: AuthContext,
    ) -> Account | None:
        query = select(Account).where(
            Account.id == account_id,
            Account.tenant_id == auth.tenant_id,
        )
        return session.exec(query).first()

    @staticmethod
    def _ensure_name_available(
        session: Session,
        *,
        tenant_id: str,
        name: str,
        exclude_account_id: str | None = None,
    ) -> None:
        query = select(Account.id).where(
            Account.tenant_id == tenant_id,
            Account.name == name,
        )
        if exclude_account_id is not None:
            query = query.where(Account.id != exclude_account_id)
        if session.exec(query.limit(1)).first() is not None:
            raise AccountAlreadyExistsError("Account name already exists in this tenant")

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

    @staticmethod
    def _ensure_account_allowed(account_id: str, auth: AuthContext) -> None:
        if not auth.can_access_account(account_id):
            raise AccountPermissionError("Account is not allowed for this user")

    @staticmethod
    def _ensure_account_admin(auth: AuthContext) -> None:
        if ACCOUNT_WILDCARD not in auth.allowed_account_ids:
            raise AccountPermissionError("Account creation requires wildcard account permission")

    @staticmethod
    def _tenant_id_for_auth(auth: AuthContext) -> str:
        return auth.tenant_id

    @staticmethod
    def _raise_account_integrity_error(exc: IntegrityError) -> None:
        constraint_name = CatalogService._extract_constraint_name(exc)
        if constraint_name == "ux_account_tenant_name":
            raise AccountAlreadyExistsError(
                "Account name already exists in this tenant"
            ) from exc
        raise exc

    @staticmethod
    def _extract_constraint_name(exc: IntegrityError) -> str | None:
        orig = getattr(exc, "orig", None)
        diag_constraint = getattr(getattr(orig, "diag", None), "constraint_name", None)
        if isinstance(diag_constraint, str) and diag_constraint:
            return diag_constraint
        if "ux_account_tenant_name" in str(exc):
            return "ux_account_tenant_name"
        return None
