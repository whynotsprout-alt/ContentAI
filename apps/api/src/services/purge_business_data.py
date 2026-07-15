from __future__ import annotations

import argparse

from agent.runtime.checkpoint import RuntimePersistence
from core.config import get_settings
from db.session import get_engine
from sqlalchemy import text

CONFIRMATION = "PURGE-CONTENTAI-BUSINESS-DATA"


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Destructively purge ContentAI business data outside Alembic migrations."
    )
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()
    if args.confirm != CONFIRMATION:
        raise SystemExit(f"Refusing purge. Pass --confirm {CONFIRMATION} exactly.")

    settings = get_settings()
    engine = get_engine(settings)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                TRUNCATE TABLE
                    executionresumerequest,
                    executionoutbox,
                    agentexecutionattempt,
                    agentevent,
                    toolexecution,
                    researchpackage,
                    agentexecution,
                    agentinvocation,
                    chatmessage,
                    chatsession,
                    memoryrecord,
                    agentversion,
                    agentprofile,
                    modelusage
                RESTART IDENTITY CASCADE
                """
            )
        )
    persistence = RuntimePersistence(settings)
    try:
        persistence.setup()
        with persistence._pool.connection() as connection:  # noqa: SLF001
            with connection.cursor() as cursor:
                for table in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
                    cursor.execute(f"DELETE FROM {table}")
    finally:
        persistence.close()
    print("ContentAI business data and checkpoints were purged.")


if __name__ == "__main__":
    main()
