from __future__ import annotations

from collections.abc import Iterator
from typing import Any

from agent.graph.state import AgentState
from agent.runtime.events import emit_event
from langchain_core.messages import AIMessage, AIMessageChunk, BaseMessage, SystemMessage
from langgraph.prebuilt import ToolNode

type ModelStreamResult = AIMessage | AIMessageChunk | BaseMessage


def build_agent_node(model: Any):
    def agent_node(state: AgentState) -> dict[str, Any]:
        emit_event("agent_node", {"status": "started"})

        messages = state.get("messages", [])
        system_prompt = state.get("system_prompt", "")
        model_input = (
            [SystemMessage(content=system_prompt), *messages]
            if system_prompt
            else messages
        )

        merged_chunks: list[AIMessageChunk] = []
        final_response: AIMessage | None = None

        for response in _iter_model_stream(model, model_input):
            if isinstance(response, AIMessageChunk):
                if merged_chunks:
                    merged_chunks = _merge_chunk(merged_chunks, response)
                else:
                    merged_chunks = [response]
            elif isinstance(response, AIMessage):
                final_response = response

        if final_response is None:
            if not merged_chunks:
                raise RuntimeError("LLM stream did not return an AI message.")
            merged_payload = merged_chunks[-1].model_dump()
            merged_payload.pop("type", None)
            merged_payload.pop("tool_call_chunks", None)
            merged_payload.pop("chunk_position", None)
            final_response = AIMessage(**merged_payload)

        emit_event("agent_node", {"status": "finished"})
        return {
            "messages": [final_response],
            "iterations": int(state.get("iterations", 0)) + 1,
        }

    return agent_node


def _iter_model_stream(
    model: Any,
    model_input: Any,
) -> Iterator[ModelStreamResult]:
    stream_fn = getattr(model, "stream", None)
    if not callable(stream_fn):
        yield _invoke_model(model, model_input)
        return
    try:
        yield from stream_fn(model_input)
    except Exception as exc:
        emit_event(
            "agent_runtime_marker",
            {
                "marker": "model_stream_fallback",
                "error_type": type(exc).__name__,
                "error": str(exc)[:800],
            },
        )
        invoke_fn = getattr(model, "invoke", None)
        if not callable(invoke_fn):
            raise RuntimeError(
                "Model streaming failed and the model client does not expose invoke(). "
                f"Cause: {type(exc).__name__}: {exc}"
            ) from exc
        try:
            response = _invoke_model(model, model_input)
        except Exception as fallback_exc:
            raise RuntimeError(
                "Model streaming failed and non-stream fallback also failed. "
                f"Stream cause: {type(exc).__name__}: {exc}; "
                f"fallback cause: {type(fallback_exc).__name__}: {fallback_exc}"
            ) from fallback_exc
        yield response


def _invoke_model(model: Any, model_input: Any) -> ModelStreamResult:
    invoke_fn = getattr(model, "invoke", None)
    if not callable(invoke_fn):
        raise RuntimeError("Model client must expose stream() or invoke().")
    response = invoke_fn(model_input)
    if isinstance(response, BaseMessage):
        return response
    return AIMessage(content=str(response or ""))


def _merge_chunk(
    chunks: list[AIMessageChunk],
    next_chunk: AIMessageChunk,
) -> list[AIMessageChunk]:
    current = chunks[-1]
    try:
        chunks[-1] = current + next_chunk
    except TypeError:
        chunks.append(next_chunk)
    return chunks


def build_tools_node(tools: list[Any]):
    tool_node = ToolNode(tools)

    def tools_node(state: AgentState) -> dict[str, Any]:
        emit_event("tools_node", {"status": "started"})
        result = tool_node.invoke(state)
        emit_event("tools_node", {"status": "finished"})
        return result

    return tools_node
