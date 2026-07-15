from __future__ import annotations

import argparse

from core.config import get_settings
from db.session import get_engine
from models.agent import AgentProfile
from models.chat import ChatSession
from models.user import AppUser
from sqlmodel import Session, select


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Explicitly claim legacy, unowned Agent profiles and sessions."
    )
    parser.add_argument("--email", required=True)
    parser.add_argument("--tenant-id", required=True)
    parser.add_argument("--confirm", action="store_true")
    args = parser.parse_args()

    with Session(get_engine(get_settings())) as session:
        user = session.exec(
            select(AppUser).where(AppUser.email_normalized == args.email.strip().lower())
        ).first()
        if user is None:
            raise SystemExit("Target user does not exist.")
        if user.tenant_id != args.tenant_id:
            raise SystemExit("Target user tenant does not match --tenant-id.")
        profiles = list(
            session.exec(
                select(AgentProfile).where(
                    AgentProfile.tenant_id == args.tenant_id,
                    AgentProfile.owner_user_id.is_(None),
                )
            ).all()
        )
        sessions = list(
            session.exec(
                select(ChatSession).where(
                    ChatSession.tenant_id == args.tenant_id,
                    ChatSession.owner_user_id != user.id,
                )
            ).all()
        )
        print(f"Would claim {len(profiles)} agents and {len(sessions)} sessions.")
        if not args.confirm:
            print("Dry run only. Re-run with --confirm after reviewing the counts.")
            return
        for profile in profiles:
            profile.owner_user_id = user.id
            profile.created_by_user_id = profile.created_by_user_id or user.id
            profile.updated_by_user_id = user.id
            session.add(profile)
        for chat in sessions:
            chat.owner_user_id = user.id
            session.add(chat)
        session.commit()
        print("Legacy ownership claim completed.")


if __name__ == "__main__":
    main()
