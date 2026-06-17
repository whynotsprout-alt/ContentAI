from __future__ import annotations

import json
from typing import Any

from articleforgeai.models.db import Account
from articleforgeai.models.schemas import (
    AccountCreate,
    AccountDetail,
    AccountSummary,
    AccountUpdate,
)
from articleforgeai.services.database import engine
from sqlmodel import Session, select

DEFAULT_ACCOUNT_SEED: dict[str, Any] = {
    "id": "gaobailie-shuo-caijing",
    "name": "高百烈说财经",
    "description": (
        "面向一二线城市 25-44 岁读者，关注商业、消费、平台、职场、"
        "AI、教育、住房和社会变化。"
    ),
    "audience": (
        "以普通读者为主，选题必须能落到工作、钱、家庭决策、消费、"
        "身份或平台规则。"
    ),
    "preferred_directions": [
        "消费变化",
        "品牌反转",
        "平台规则变化",
        "职场组织变化",
        "AI 进入普通生活",
        "教育和家庭决策",
        "房子、物业、价格压力",
    ],
    "boundaries": [
        "不做纯融资消息",
        "不做纯股市行情",
        "不做纯技术参数",
        "不制造性别对立",
        "宏观和法律题必须转译成普通人为什么要关心",
    ],
    "viral_patterns": [
        "宏大变化落到我",
        "AI 从技术变工具",
        "消费品牌反转",
        "中产和价格压力",
        "平台规则改变普通人",
        "地方与国货新叙事",
    ],
    "style_prompt": (
        "用克制、清晰、可验证的商业观察口吻写作，避免情绪化标题党。"
    ),
}


class CatalogService:
    def __init__(self) -> None:
        pass

    def bootstrap_from_files_if_needed(self) -> None:
        with Session(engine) as session:
            self._ensure_default_account(session)

    def list_accounts(self) -> list[AccountSummary]:
        with Session(engine) as session:
            rows = session.exec(select(Account).order_by(Account.id)).all()
            return [self._account_to_summary(account) for account in rows]

    def get_account(self, account_id: str) -> dict[str, Any]:
        with Session(engine) as session:
            account = session.get(Account, account_id)
            if account is None:
                raise FileNotFoundError(account_id)
            return self._account_to_dict(account)

    def create_account(self, payload: AccountCreate) -> AccountDetail:
        with Session(engine) as session:
            if session.get(Account, payload.id) is not None:
                raise FileExistsError(payload.id)
            account = self._payload_to_record(payload.model_dump())
            if account is None:
                raise ValueError("Account id and name are required")
            session.add(account)
            session.commit()
            session.refresh(account)
            return self._account_to_detail(account)

    def update_account(self, account_id: str, payload: AccountUpdate) -> AccountDetail:
        with Session(engine) as session:
            account = session.get(Account, account_id)
            if account is None:
                raise FileNotFoundError(account_id)

            updates = payload.model_dump(exclude_unset=True)
            raw_profile = self._load_raw_profile(account.raw_profile)
            for key, value in updates.items():
                if key in {"preferred_directions", "boundaries", "viral_patterns"}:
                    setattr(account, key, self._encode_list(value))
                    continue

                if key in {"hotspot_platforms", "topic_filter_prompt", "topic_scoring_prompt"}:
                    # These fields now belong to system config and should not be persisted
                    # on a per-account basis.
                    continue

                if key == "name":
                    account.name = self._decode_text(value)
                    continue
                if key == "description":
                    account.description = self._decode_text(value)
                    continue
                if key == "audience":
                    account.audience = self._decode_text(value)
                    continue
                if key == "style_prompt":
                    account.style_prompt = self._decode_text(value)
                    continue
                if value is None:
                    raw_profile.pop(key, None)
                else:
                    raw_profile[key] = value

            account.raw_profile = (
                json.dumps(raw_profile, ensure_ascii=False) if raw_profile else "{}"
            )
            session.add(account)
            session.commit()
            session.refresh(account)
            return self._account_to_detail(account)

    def delete_account(self, account_id: str) -> None:
        with Session(engine) as session:
            account = session.get(Account, account_id)
            if account is None:
                raise FileNotFoundError(account_id)
            session.delete(account)
            session.commit()

    @staticmethod
    def _decode_text(value: Any) -> str:
        return value if isinstance(value, str) else ""

    @staticmethod
    def _decode_list(value: Any) -> list[str]:
        if not isinstance(value, str):
            if isinstance(value, list):
                return [CatalogService._decode_text(item) for item in value]
            return []
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        if not isinstance(parsed, list):
            return []
        return [CatalogService._decode_text(item) for item in parsed]

    @staticmethod
    def _encode_list(value: Any) -> str:
        if isinstance(value, list):
            cleaned = [str(item) for item in value if str(item).strip()]
            return json.dumps(cleaned, ensure_ascii=False)
        if isinstance(value, str):
            return value
        return "[]"

    def _account_to_dict(self, account: Account) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": account.id,
            "name": account.name,
            "description": account.description,
            "audience": account.audience,
            "preferred_directions": self._decode_list(account.preferred_directions),
            "boundaries": self._decode_list(account.boundaries),
            "viral_patterns": self._decode_list(account.viral_patterns),
            "style_prompt": account.style_prompt,
        }
        raw = self._load_raw_profile(account.raw_profile)
        for key, value in raw.items():
            if (
                key in payload
                or key in {"raw_profile", "scoring_rules"}
                or key in {"hotspot_platforms", "topic_filter_prompt", "topic_scoring_prompt"}
            ):
                continue
            payload[key] = value
        return payload

    def _account_to_summary(self, account: Account) -> AccountSummary:
        return AccountSummary(
            id=account.id,
            name=account.name,
            description=account.description,
            style_prompt=self._decode_text(account.style_prompt),
        )

    def _account_to_detail(self, account: Account) -> AccountDetail:
        return AccountDetail(**self._account_to_dict(account))

    def _ensure_default_account(self, session: Session) -> None:
        try:
            has_rows = session.exec(select(Account.id)).first()
        except Exception as exc:
            raise RuntimeError("Database bootstrap failed: cannot read Account table.") from exc

        if has_rows is not None:
            return

        account = self._payload_to_record(DEFAULT_ACCOUNT_SEED)
        if account is None:
            raise RuntimeError("Default account seed is invalid.")
        session.add(account)
        session.commit()

    @staticmethod
    def _load_raw_profile(value: str) -> dict[str, Any]:
        if not value:
            return {}
        try:
            raw = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return raw if isinstance(raw, dict) else {}

    @staticmethod
    def _payload_to_record(payload: dict[str, Any]) -> Account | None:
        account_id = CatalogService._decode_text(payload.get("id"))
        name = CatalogService._decode_text(payload.get("name"))
        if not account_id or not name:
            return None
        excluded = {
            "id",
            "name",
            "description",
            "audience",
            "preferred_directions",
            "boundaries",
            "viral_patterns",
            "style_prompt",
            "hotspot_platforms",
            "topic_filter_prompt",
            "topic_scoring_prompt",
        }
        raw = {key: value for key, value in payload.items() if key not in excluded}
        for key in {"hotspot_platforms", "topic_filter_prompt", "topic_scoring_prompt"}:
            if key in payload:
                value = payload.get(key)
                if key == "hotspot_platforms":
                    encoded = CatalogService._encode_list(value)
                    if encoded != "[]":
                        raw[key] = encoded
                    continue
                text_value = CatalogService._decode_text(value).strip()
                if text_value:
                    raw[key] = text_value
        return Account(
            id=account_id,
            name=name,
            description=CatalogService._decode_text(payload.get("description")),
            audience=CatalogService._decode_text(payload.get("audience")),
            preferred_directions=CatalogService._encode_list(payload.get("preferred_directions")),
            boundaries=CatalogService._encode_list(payload.get("boundaries")),
            viral_patterns=CatalogService._encode_list(payload.get("viral_patterns")),
            style_prompt=CatalogService._decode_text(payload.get("style_prompt")),
            raw_profile=json.dumps(raw, ensure_ascii=False),
        )


catalog_service = CatalogService()
