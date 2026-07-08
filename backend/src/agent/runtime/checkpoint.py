from __future__ import annotations

import logging
from contextlib import ExitStack
from typing import Any

from core.config import get_settings
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool
from sqlalchemy import text
from sqlmodel import Session

_PERSISTENCE_STACK = ExitStack()
_CHECKPOINTER: PostgresSaver | None = None
_STORE: PostgresStore | None = None
logger = logging.getLogger(__name__)


def build_checkpointer() -> PostgresSaver:
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        pool = _PERSISTENCE_STACK.enter_context(_postgres_pool("contentai-checkpointer"))
        checkpointer = PostgresSaver(pool)
        checkpointer.setup()
        _CHECKPOINTER = checkpointer
    return _CHECKPOINTER


def build_store() -> PostgresStore:
    global _STORE
    if _STORE is None:
        pool = _PERSISTENCE_STACK.enter_context(_postgres_pool("contentai-store"))
        store = PostgresStore(conn=pool)
        store.setup()
        _STORE = store
    return _STORE


def close_runtime_persistence() -> None:
    global _CHECKPOINTER, _STORE, _PERSISTENCE_STACK
    _PERSISTENCE_STACK.close()
    _PERSISTENCE_STACK = ExitStack()
    _CHECKPOINTER = None
    _STORE = None


def checkpoint_messages(checkpointer: Any, *, thread_id: str) -> list[BaseMessage]:
    checkpoint = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
    if checkpoint is None:
        return []
    values = checkpoint.checkpoint.get("channel_values", {})
    messages = values.get("messages", [])
    return [message for message in messages if isinstance(message, BaseMessage)]


def clear_thread_persistence(*, thread_id: str, session: Session | None = None) -> None:
    if session is not None:
        _clear_thread_persistence_in_transaction(thread_id=thread_id, session=session)
        return

    if _CHECKPOINTER is not None and hasattr(_CHECKPOINTER, "delete_thread"):
        _CHECKPOINTER.delete_thread(thread_id)
    else:
        raise RuntimeError("LangGraph checkpointer does not support thread deletion.")

    if _STORE is not None:
        _clear_session_store_with_adapter(thread_id=thread_id, store=_STORE)


def _clear_thread_persistence_in_transaction(*, thread_id: str, session: Session) -> None:
    for table_name in ("checkpoint_writes", "checkpoint_blobs", "checkpoints"):
        if _table_exists(session, table_name):
            session.execute(
                text(f"DELETE FROM {table_name} WHERE thread_id = :thread_id"),
                {"thread_id": thread_id},
            )

    if not _table_exists(session, "store"):
        return

    prefix = _session_store_prefix(thread_id)
    params = {"prefix": prefix, "child_prefix": f"{prefix}.%"}
    if _table_exists(session, "store_vectors"):
        session.execute(
            text(
                """
                DELETE FROM store_vectors
                WHERE prefix = :prefix OR prefix LIKE :child_prefix
                """
            ),
            params,
        )
    session.execute(
        text(
            """
            DELETE FROM store
            WHERE prefix = :prefix OR prefix LIKE :child_prefix
            """
        ),
        params,
    )


def _clear_session_store_with_adapter(*, thread_id: str, store: Any) -> None:
    prefix = ("sessions", thread_id)
    for namespace in store.list_namespaces(prefix=prefix, limit=1000):
        for item in store.search(namespace, limit=1000):
            store.delete(namespace, item.key)


def _table_exists(session: Session, table_name: str) -> bool:
    row = session.execute(
        text("SELECT to_regclass(:table_name) IS NOT NULL"),
        {"table_name": table_name},
    ).first()
    return bool(row and row[0])


def _session_store_prefix(thread_id: str) -> str:
    return f"sessions.{thread_id}"


def _postgres_conn_string() -> str:
    database_url = get_settings().database.url or ""
    return (
        database_url.replace("postgresql+psycopg2://", "postgresql://", 1)
        .replace("postgresql+psycopg://", "postgresql://", 1)
    )


def _postgres_pool(name: str) -> ConnectionPool:
    return ConnectionPool(
        _postgres_conn_string(),
        min_size=1,
        max_size=4,
        max_idle=60,
        max_lifetime=600,
        check=ConnectionPool.check_connection,
        name=name,
        kwargs={
            "autocommit": True,
            "prepare_threshold": 0,
            "row_factory": dict_row,
        },
    )
