from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any

from agent.runtime.checkpoint import RuntimePersistence
from core.config import Settings, get_settings
from db.session import get_engine
from sqlalchemy import text

CONFIRMATION = "PURGE-CONTENTAI-BUSINESS-DATA"
MAX_BATCH_SIZE = 1000

# Child tables precede their parents. Names are a fixed allowlist, never CLI input.
BUSINESS_TABLES: tuple[tuple[str, str | None], ...] = (
    ("executionresumerequest", "created_at"),
    ("executionoutbox", "created_at"),
    ("agentexecutionattempt", "started_at"),
    ("sideeffectreceipt", "created_at"),
    ("checkpointdeletionoutbox", "created_at"),
    ("toolexecution", "created_at"),
    ("researchpackage", "created_at"),
    ("chatmessage", "created_at"),
    ("agentexecution", "created_at"),
    ("agentinvocation", "created_at"),
    ("chatsession", "created_at"),
    ("memoryrecord", "created_at"),
    ("agentversion", "created_at"),
    ("agentprofile", "created_at"),
    ("modelusage", "created_at"),
    ("adminauditlog", "created_at"),
    ("serviceheartbeat", "created_at"),
)


def _validate_batch_size(batch_size: int) -> None:
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")


def _candidate_count(
    connection: Any,
    table: str,
    timestamp_column: str | None,
    cutoff: datetime | None,
) -> int:
    if cutoff is None or timestamp_column is None:
        query = text(f"SELECT count(*) FROM {table}")
        return int(connection.execute(query).scalar_one())
    query = text(f"SELECT count(*) FROM {table} WHERE {timestamp_column} < :cutoff")
    return int(connection.execute(query, {"cutoff": cutoff}).scalar_one())


def _delete_batch(
    connection: Any,
    table: str,
    timestamp_column: str | None,
    cutoff: datetime | None,
    batch_size: int,
) -> int:
    predicate = ""
    params: dict[str, object] = {"batch_size": batch_size}
    if cutoff is not None and timestamp_column is not None:
        predicate = f" WHERE {timestamp_column} < :cutoff"
        params["cutoff"] = cutoff
    result = connection.execute(
        text(
            f"DELETE FROM {table} WHERE ctid IN "
            f"(SELECT ctid FROM {table}{predicate} ORDER BY ctid LIMIT :batch_size)"
        ),
        params,
    )
    return int(result.rowcount or 0)


def purge_business_data(
    *,
    dry_run: bool = False,
    batch_size: int = 100,
    retention_cutoff: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    _validate_batch_size(batch_size)
    engine = get_engine(settings or get_settings())
    summary: dict[str, Any] = {"dry_run": dry_run, "batch_size": batch_size, "tables": {}}
    with engine.begin() as connection:
        for table, timestamp_column in BUSINESS_TABLES:
            count = _candidate_count(connection, table, timestamp_column, retention_cutoff)
            deleted = 0
            if not dry_run:
                while True:
                    batch = _delete_batch(
                        connection,
                        table,
                        timestamp_column,
                        retention_cutoff,
                        batch_size,
                    )
                    deleted += batch
                    if batch < batch_size:
                        break
            summary["tables"][table] = {"candidates": count, "deleted": deleted}

    persistence = RuntimePersistence(settings or get_settings())
    try:
        summary["checkpoints"] = persistence.purge_checkpoint_data(
            batch_size=batch_size,
            dry_run=dry_run,
            retention_cutoff=retention_cutoff,
        )
    finally:
        persistence.close()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Bounded ContentAI business-data purge.")
    parser.add_argument("--confirm")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--retention-cutoff", type=datetime.fromisoformat)
    args = parser.parse_args()
    try:
        _validate_batch_size(args.batch_size)
    except ValueError as exc:
        parser.error(str(exc))
    if not args.dry_run and args.confirm != CONFIRMATION:
        raise SystemExit(f"Refusing purge. Pass --confirm {CONFIRMATION} exactly.")
    summary = purge_business_data(
        dry_run=args.dry_run,
        batch_size=args.batch_size,
        retention_cutoff=args.retention_cutoff,
    )
    print(summary)


if __name__ == "__main__":
    main()
