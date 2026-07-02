from __future__ import annotations

from dataclasses import dataclass

from agent.context.window import trim_context_window
from agent.memory.retriever import render_memories
from agent.memory.types import MemoryEntry
from agent.prompts.registry import build_system_prompt
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
    ) -> AgentContext:
        window = trim_context_window(messages)
        system_prompt = build_system_prompt(
            account_name=account.name,
            account_description=account.description,
            account_instructions=account.instructions,
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
