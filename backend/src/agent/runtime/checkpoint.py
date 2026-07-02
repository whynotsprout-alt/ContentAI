from __future__ import annotations

from typing import Any

from langchain_core.messages import BaseMessage
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.store.memory import InMemoryStore


def build_checkpointer() -> InMemorySaver:
    return InMemorySaver()


def build_store() -> InMemoryStore:
    return InMemoryStore()


def checkpoint_messages(checkpointer: Any, *, thread_id: str) -> list[BaseMessage]:
    checkpoint = checkpointer.get_tuple({"configurable": {"thread_id": thread_id}})
    if checkpoint is None:
        return []
    values = checkpoint.checkpoint.get("channel_values", {})
    messages = values.get("messages", [])
    return [message for message in messages if isinstance(message, BaseMessage)]
