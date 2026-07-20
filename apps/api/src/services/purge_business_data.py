from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from agent.runtime.checkpoint import RuntimePersistence
from core.config import Settings, get_settings
from db.session import get_engine
from sqlalchemy import bindparam, text

CONFIRMATION = "PURGE-CONTENTAI-BUSINESS-DATA"
MAX_BATCH_SIZE = 1000


@dataclass(frozen=True)
class TableSpec:
    name: str
    timestamp_column: str | None
    references: tuple[tuple[str, str, str], ...] = ()


# Leaf-to-root fixed allowlist. Parent rows are eligible only when no retained
# child references them, preventing CASCADE from crossing a retention cutoff.
BUSINESS_TABLES: tuple[TableSpec, ...] = (
    TableSpec("executionresumerequest", "created_at"),
    TableSpec("executionoutbox", "created_at"),
    TableSpec("agentexecutionattempt", "started_at"),
    TableSpec("sideeffectreceipt", "created_at"),
    TableSpec("checkpointdeletionoutbox", "created_at"),
    TableSpec("toolexecution", "created_at"),
    TableSpec("researchpackage", "created_at"),
    TableSpec(
        "chatmessage",
        "created_at",
        (("executionresumerequest", "message_id", "id"),),
    ),
    TableSpec(
        "agentexecution",
        "created_at",
        (
            ("executionresumerequest", "execution_id", "id"),
            ("executionoutbox", "execution_id", "id"),
            ("agentexecutionattempt", "execution_id", "id"),
            ("sideeffectreceipt", "execution_id", "id"),
            ("toolexecution", "execution_id", "id"),
            ("researchpackage", "execution_id", "id"),
            ("chatmessage", "execution_id", "id"),
        ),
    ),
    TableSpec(
        "agentinvocation",
        "created_at",
        (
            ("agentexecution", "invocation_id", "id"),
            ("chatmessage", "invocation_id", "id"),
        ),
    ),
    TableSpec("memoryrecord", "created_at"),
    TableSpec(
        "chatsession",
        "created_at",
        (
            ("agentinvocation", "session_id", "id"),
            ("agentexecution", "session_id", "id"),
            ("chatmessage", "session_id", "id"),
            ("memoryrecord", "session_id", "id"),
            ("researchpackage", "session_id", "id"),
        ),
    ),
    TableSpec(
        "agentversion",
        "created_at",
        (
            ("chatsession", "agent_version_id", "id"),
            ("agentexecution", "agent_version_id", "id"),
            ("researchpackage", "agent_version_id", "id"),
        ),
    ),
    TableSpec(
        "agentprofile",
        "created_at",
        (
            ("agentversion", "agent_id", "id"),
            ("chatsession", "agent_id", "id"),
            ("agentinvocation", "agent_id", "id"),
            ("memoryrecord", "agent_id", "id"),
        ),
    ),
    TableSpec("modelusage", "created_at"),
    TableSpec("adminauditlog", "created_at"),
    TableSpec("serviceheartbeat", "created_at"),
)


def _validate_batch_size(batch_size: int) -> None:
    if batch_size < 1 or batch_size > MAX_BATCH_SIZE:
        raise ValueError(f"batch_size must be between 1 and {MAX_BATCH_SIZE}")


def _candidate_query(spec: TableSpec, *, retention: bool) -> Any:
    predicates: list[str] = []
    if retention and spec.timestamp_column is not None:
        predicates.append(f"candidate.{spec.timestamp_column} < :cutoff")
    for child_table, child_column, parent_column in spec.references:
        predicates.append(
            "NOT EXISTS ("
            f"SELECT 1 FROM {child_table} AS child "
            f"WHERE child.{child_column} = candidate.{parent_column}"
            ")"
        )
    where = " WHERE " + " AND ".join(predicates) if predicates else ""
    return text(
        f"SELECT candidate.id FROM {spec.name} AS candidate{where} "
        "ORDER BY candidate.id LIMIT :probe_limit"
    )


def _probe_candidates(
    connection: Any,
    spec: TableSpec,
    *,
    batch_size: int,
    cutoff: datetime | None,
) -> list[str]:
    params: dict[str, object] = {"probe_limit": batch_size + 1}
    if cutoff is not None:
        params["cutoff"] = cutoff
    return [
        str(row[0])
        for row in connection.execute(
            _candidate_query(spec, retention=cutoff is not None),
            params,
        ).all()
    ]


def _delete_candidates(connection: Any, spec: TableSpec, ids: list[str]) -> int:
    if not ids:
        return 0
    statement = text(f"DELETE FROM {spec.name} WHERE id IN :ids").bindparams(
        bindparam("ids", expanding=True)
    )
    result = connection.execute(statement, {"ids": ids})
    return int(result.rowcount or 0)


def purge_business_data(
    *,
    dry_run: bool = False,
    batch_size: int = 100,
    retention_cutoff: datetime | None = None,
    settings: Settings | None = None,
) -> dict[str, Any]:
    _validate_batch_size(batch_size)
    resolved_settings = settings or get_settings()
    engine = get_engine(resolved_settings)
    summary: dict[str, Any] = {"dry_run": dry_run, "batch_size": batch_size, "tables": {}}
    for spec in BUSINESS_TABLES:
        with engine.begin() as connection:
            probed = _probe_candidates(
                connection,
                spec,
                batch_size=batch_size,
                cutoff=retention_cutoff,
            )
            candidate_ids = probed[:batch_size]
            deleted = 0 if dry_run else _delete_candidates(connection, spec, candidate_ids)
        summary["tables"][spec.name] = {
            "candidate_count": len(candidate_ids),
            "has_more": len(probed) > batch_size,
            "deleted": deleted,
        }

    persistence = RuntimePersistence(resolved_settings)
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
