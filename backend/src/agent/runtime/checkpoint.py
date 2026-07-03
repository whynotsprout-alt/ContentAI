from __future__ import annotations

from contextlib import ExitStack
from typing import Any

from core.config import get_settings
from langchain_core.messages import BaseMessage
from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.store.postgres import PostgresStore

_PERSISTENCE_STACK = ExitStack()
_CHECKPOINTER: PostgresSaver | None = None
_STORE: PostgresStore | None = None


def build_checkpointer() -> PostgresSaver:
    global _CHECKPOINTER
    if _CHECKPOINTER is None:
        checkpointer = _PERSISTENCE_STACK.enter_context(
            PostgresSaver.from_conn_string(_postgres_conn_string())
        )
        checkpointer.setup()
        _CHECKPOINTER = checkpointer
    return _CHECKPOINTER


def build_store() -> PostgresStore:
    global _STORE
    if _STORE is None:
        store = _PERSISTENCE_STACK.enter_context(
            PostgresStore.from_conn_string(_postgres_conn_string())
        )
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


def _postgres_conn_string() -> str:
    database_url = get_settings().database_url
    return (
        database_url.replace("postgresql+psycopg2://", "postgresql://", 1)
        .replace("postgresql+psycopg://", "postgresql://", 1)
    )
