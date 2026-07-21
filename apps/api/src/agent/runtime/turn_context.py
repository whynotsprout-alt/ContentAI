from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from agent.context.assembler import AgentContext, ContextAssembler
from agent.context.window import TokenCounter
from langchain_core.messages import BaseMessage
from memory import LongTermMemory, MemoryRepository, ShortTermMemory
from memory.types import MemoryEntry


@dataclass(frozen=True)
class TurnPromptInputs:
    messages: list[BaseMessage]
    short_term_summary: str
    long_term_memories: list[MemoryEntry]


def load_turn_prompt_inputs(
    db_session: Any,
    *,
    session_id: str,
    user_id: str,
    agent_id: str,
    focus_message: str,
    pending_message: BaseMessage | None = None,
) -> TurnPromptInputs:
    """Read the same non-mutating prompt inputs before and after turn persistence."""
    repository = MemoryRepository(db_session)
    short_term = ShortTermMemory(repository)
    summary, messages = short_term.load(
        db_session,
        session_id=session_id,
        user_id=user_id,
        refresh_if_missing=False,
        touch=False,
    )
    if pending_message is not None:
        messages = [*messages, pending_message]
    recalled = LongTermMemory(repository).recall(
        agent_id,
        focus_message,
        user_id=user_id,
        limit=8,
        touch=False,
    )
    return TurnPromptInputs(
        messages=messages,
        short_term_summary=summary,
        long_term_memories=recalled,
    )


def assemble_turn_context(
    *,
    context_assembler: ContextAssembler,
    agent_profile: Any,
    agent_version: Any,
    prompt_inputs: TurnPromptInputs,
    tool_names: list[str],
    focus_message: str,
    user_id: str,
    conversation_id: str,
    execution_id: str,
    research_package: Any | None,
    token_counter: TokenCounter | None,
) -> AgentContext:
    """Use one assembly path for preflight and the worker's real model input."""
    return context_assembler.assemble(
        agent_profile=agent_profile,
        agent_version=agent_version,
        messages=prompt_inputs.messages,
        short_term_summary=prompt_inputs.short_term_summary,
        long_term_memories=prompt_inputs.long_term_memories,
        tool_names=tool_names,
        focus_message=focus_message,
        user_id=user_id,
        conversation_id=conversation_id,
        run_id=execution_id,
        permissions=tool_names,
        research_package=research_package,
        token_counter=token_counter,
    )


__all__ = ["TurnPromptInputs", "assemble_turn_context", "load_turn_prompt_inputs"]
