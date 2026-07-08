from __future__ import annotations

from dataclasses import dataclass

from agent.context.window import trim_context_window
from memory.retriever import render_memories
from memory.types import MemoryEntry
from agent.prompts.registry import build_system_prompt
from core.config import get_settings
from core.hotspot_sources import render_hotspot_sources
from langchain_core.messages import BaseMessage
from models.account import Account


@dataclass(frozen=True)
class AgentContext:
    system_prompt: str
    messages: list[BaseMessage]
    short_term_summary: str
    long_term_memories: list[MemoryEntry]


class ContextAssembler:
    def assemble(
        self,
        *,
        account: Account,
        messages: list[BaseMessage],
        short_term_summary: str,
        long_term_memories: list[MemoryEntry],
        tool_names: list[str],
        focus_message: str | None = None,
    ) -> AgentContext:
        settings = get_settings()
        window = trim_context_window(
            messages,
            limit=settings.agent.context_max_messages,
            min_focused_retain=settings.agent.context_min_focused_messages,
            focus_message=focus_message,
        )
        system_prompt = build_system_prompt(
            account_id=account.id,
            account_name=account.name,
            account_positioning=account.positioning,
            topic_scoring_prompt=account.topic_scoring_prompt,
            content_creation_prompt=account.content_creation_prompt,
            allowed_hotspot_sources=render_hotspot_sources(account.hotspot_sources),
            short_term_summary=short_term_summary,
            long_term_memory=render_memories(long_term_memories),
            tool_names=tool_names,
        )
        return AgentContext(
            system_prompt=system_prompt,
            messages=window,
            short_term_summary=short_term_summary,
            long_term_memories=long_term_memories,
        )
