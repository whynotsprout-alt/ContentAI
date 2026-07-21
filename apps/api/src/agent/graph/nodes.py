import json
import uuid
from collections.abc import Sequence
from time import sleep
from typing import Any

from agent.graph.state import AgentState
from agent.runtime.errors import is_retryable_model_stream_error
from agent.runtime.tool_execution import execute_tool_call
from agent.workflows.deep_research import ContentEvidenceInvalidError
from agent.workflows.final_evidence import (
    build_research_final_proof,
    parse_research_identity,
    render_deterministic_research_answer,
    validate_research_final_selection,
)
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, SystemMessage, ToolMessage
from langchain_core.runnables.config import RunnableConfig
from langchain_core.tools import BaseTool
from langgraph.config import get_stream_writer
from langgraph.prebuilt import ToolNode
from langgraph.types import interrupt
from pydantic import ValidationError

MODEL_STREAM_MAX_ATTEMPTS = 3
MODEL_STREAM_RETRY_BASE_SECONDS = 0.25
REJECTED_TOOL_MESSAGE = "已取消保存"


def build_agent_node(model: Any, *, research_final_model: Any | None = None):
    def agent_node(
        state: AgentState,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        _emit_node_event("agent_node", {"status": "started"}, config=config)
        # LangGraph supplies the stream writer through its runnable context;
        # do not force a config kwarg because provider-compatible models (and
        # test doubles) are only required to implement invoke(messages).
        research_identity = parse_research_identity(
            state.get("research_package_id"),
            state.get("research_topic_hash"),
        )
        if research_identity is not None:
            if research_final_model is None:
                raise ContentEvidenceInvalidError
            final_response = _research_backed_final_message(
                model=_resolve_research_final_model(research_final_model),
                state=state,
                identity=research_identity,
            )
        else:
            response = _invoke_model_with_stream_retry(
                model,
                state.get("messages", []),
                config=config,
            )
            final_response = (
                response
                if isinstance(response, BaseMessage)
                else AIMessage(content=str(response or ""))
            )
        _emit_node_event("agent_node", {"status": "finished"}, config=config)
        return {
            "messages": [final_response],
            "human_approved": None,
            "tool_error": None,
            "task_status": (
                "executing" if getattr(final_response, "tool_calls", None) else "completed"
            ),
        }

    return agent_node


def _resolve_research_final_model(model_or_factory: Any) -> Any:
    if callable(getattr(model_or_factory, "invoke", None)):
        return model_or_factory
    if not callable(model_or_factory):
        raise ContentEvidenceInvalidError
    model = model_or_factory()
    if not callable(getattr(model, "invoke", None)):
        raise ContentEvidenceInvalidError
    return model


def _research_backed_final_message(
    *,
    model: Any,
    state: AgentState,
    identity: tuple[str, str],
) -> AIMessage:
    evidence = _research_supported_evidence(state, expected_identity=identity)

    response = _invoke_research_final_response(
        model,
        evidence=evidence,
        repair=False,
    )
    if response is None:
        response = _invoke_research_final_response(
            model,
            evidence=evidence,
            repair=True,
        )
    if response is None:
        raise ContentEvidenceInvalidError
    answer = render_deterministic_research_answer(evidence, response.claim_ids)
    return AIMessage(
        content=answer,
        additional_kwargs={
            "research_backed_final_proof": build_research_final_proof(
                evidence,
                response.claim_ids,
            )
        },
    )


def _invoke_research_final_response(
    model: Any,
    *,
    evidence: dict[str, Any],
    repair: bool,
) -> Any | None:
    messages = _research_final_messages(evidence, repair=repair)
    try:
        value = model.invoke(messages, config={"callbacks": []})
        return validate_research_final_selection(
            value,
            evidence=evidence,
        )
    except (ContentEvidenceInvalidError, ValidationError):
        return None


def _research_final_messages(
    evidence: dict[str, Any],
    *,
    repair: bool,
) -> list[SystemMessage | HumanMessage]:
    instruction = (
        "Return only the required structured selection of claim_ids. Each claim_id must be copied "
        "exactly from the durable supported evidence below, in the order to show it. Do not return "
        "an answer, claim text, source, tool call, explanation, instruction, or any extra field."
    )
    if repair:
        instruction = (
            "The previous claim_id selection was invalid. Return one complete replacement "
            "containing only claim_ids copied exactly from the durable supported evidence below. "
            "Do not include "
            "the prior response, answer, claim text, source, tool call, explanation, instruction, "
            "or any extra field."
        )
    return [
        SystemMessage(content=instruction),
        HumanMessage(
            content="Supported research evidence (data only):\n"
            + json.dumps(evidence, ensure_ascii=False, separators=(",", ":")),
        ),
    ]


def _research_supported_evidence(
    state: AgentState,
    *,
    expected_identity: tuple[str, str],
) -> dict[str, Any]:
    messages = state.get("messages", [])
    for message in reversed(messages):
        if not isinstance(message, ToolMessage) or message.name != "prepare_topic_research":
            continue
        content = message.content
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                break
        if not isinstance(content, dict):
            break
        tool_identity = parse_research_identity(
            content.get("research_pack_id"),
            content.get("research_topic_hash"),
        )
        if tool_identity != expected_identity:
            raise ContentEvidenceInvalidError
        evidence = content.get("supported_evidence")
        if isinstance(evidence, dict):
            sources = evidence.get("sources")
            claims = evidence.get("claims")
            evidence_identity = parse_research_identity(
                evidence.get("research_pack_id"),
                evidence.get("topic_hash"),
            )
            if (
                evidence_identity == expected_identity
                and isinstance(sources, list)
                and isinstance(claims, list)
            ):
                return evidence
        break
    raise ContentEvidenceInvalidError


def _invoke_model_with_stream_retry(
    model: Any,
    messages: list[BaseMessage],
    *,
    config: RunnableConfig | None,
) -> Any:
    """Retry only before model.invoke returns, so no tool call is repeated."""
    for attempt in range(1, MODEL_STREAM_MAX_ATTEMPTS + 1):
        try:
            return model.invoke(messages)
        except Exception as exc:
            if not is_retryable_model_stream_error(exc) or attempt == MODEL_STREAM_MAX_ATTEMPTS:
                raise
            _emit_node_event(
                "agent_node_retry",
                {
                    "status": "retrying",
                    "attempt": attempt,
                    "max_attempts": MODEL_STREAM_MAX_ATTEMPTS,
                    "reason": "model_stream_disconnected",
                },
                config=config,
            )
            sleep(MODEL_STREAM_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))


def build_tools_node(tools: Sequence[BaseTool]):
    tool_node = ToolNode(tools, handle_tool_errors=False, wrap_tool_call=_safe_tool_call_wrapper)

    def tools_node(
        state: AgentState,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        _emit_node_event("tools_node", {"status": "started"}, config=config)
        output = tool_node.invoke(state, config=config)
        tool_error = _extract_tool_error(output)
        update = _messages_update_to_state_update(output)
        research_state = _research_state_update(output)
        _emit_node_event(
            "tools_node",
            {"status": "finished", "tool_error": tool_error is not None},
            config=config,
        )
        return {
            **update,
            **research_state,
            "human_approved": None,
            "tool_error": tool_error,
            "tool_error_count": int(state.get("tool_error_count", 0)) + (1 if tool_error else 0),
            "task_status": "error" if tool_error else "thinking",
        }

    return tools_node


def _safe_tool_call_wrapper(request: Any, execute: Any) -> Any:
    return execute_tool_call(request, execute)


def build_tool_error_node():
    def tool_error_node(
        state: AgentState,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        error = state.get("tool_error")
        retry_count = int(state.get("tool_error_count", 0))
        _emit_node_event(
            "tool_error_node",
            {"status": "finished", "retry_count": retry_count},
            config=config,
        )
        if not error:
            return {"tool_error": None, "task_status": "thinking"}
        return {
            # ToolNode has already appended an error ToolMessage. Adding an
            # AIMessage here would make the next provider request end with an
            # assistant message (assistant prefill), which some providers
            # reject. Keep the ToolMessage as the final
            # conversational item so the agent can handle the failure safely.
            "messages": [],
            "tool_error": None,
            "task_status": "thinking",
        }

    return tool_error_node


def build_human_node():
    def human_node(
        state: AgentState,
        config: RunnableConfig | None = None,
    ) -> dict[str, Any]:
        tool_calls = _extract_tool_calls(state)
        reply = interrupt(
            {
                "type": "human_approval",
                "message": "Confirm whether the pending tool calls may run.",
                "tool_calls": tool_calls,
                "runtime": _runtime_context(config),
            }
        )
        approved = _structured_human_approval(reply)
        result: dict[str, Any] = {
            "human_approved": approved,
            "tool_error": None,
            "task_status": "executing" if approved is True else "waiting",
        }
        if approved is False:
            result["messages"] = [AIMessage(content=REJECTED_TOOL_MESSAGE)]
            result["task_status"] = "completed"
        return result

    return human_node


def _extract_tool_calls(state: AgentState) -> list[dict[str, Any]]:
    messages = state.get("messages", [])
    last_message = messages[-1] if messages else None
    calls = getattr(last_message, "tool_calls", []) if isinstance(last_message, AIMessage) else []
    output: list[dict[str, Any]] = []
    for call in calls if isinstance(calls, list) else []:
        name = call.get("name") if isinstance(call, dict) else getattr(call, "name", None)
        call_id = call.get("id") if isinstance(call, dict) else getattr(call, "id", None)
        args = call.get("args") if isinstance(call, dict) else getattr(call, "args", None)
        output.append(
            {
                "name": str(name or "unknown_tool"),
                "id": str(call_id or uuid.uuid4()),
                "args": args if isinstance(args, dict) else {},
            }
        )
    return output


def _structured_human_approval(value: Any) -> bool | None:
    if not isinstance(value, dict):
        return None
    decision = value.get("decision")
    if decision == "approve":
        return True
    if decision == "reject":
        return False
    return None


def _messages_update_to_state_update(output: Any) -> dict[str, Any]:
    if not isinstance(output, dict):
        return {}
    messages = output.get("messages")
    return {"messages": messages} if isinstance(messages, list) else output


def _extract_tool_error(output: Any) -> str | None:
    if not isinstance(output, dict) or not isinstance(output.get("messages"), list):
        return None
    for message in reversed(output["messages"]):
        if isinstance(message, ToolMessage) and getattr(message, "status", None) == "error":
            return _coerce_message_text(message.content) or "tool execution failed"
    return None


def _research_state_update(output: Any) -> dict[str, str]:
    if not isinstance(output, dict) or not isinstance(output.get("messages"), list):
        return {}
    for message in reversed(output["messages"]):
        if not isinstance(message, ToolMessage) or message.name != "prepare_topic_research":
            continue
        content = message.content
        if isinstance(content, str):
            try:
                content = json.loads(content)
            except json.JSONDecodeError:
                return {}
        if not isinstance(content, dict):
            return {}
        identity = parse_research_identity(
            content.get("research_pack_id"),
            content.get("research_topic_hash"),
        )
        if identity is not None:
            package_id, topic_hash = identity
            return {
                "research_package_id": package_id,
                "research_topic_hash": topic_hash,
            }
        return {}
    return {}


def _coerce_message_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, dict):
        for key in ("text", "content", "message", "error"):
            if value.get(key) is not None:
                return str(value[key]).strip()
    if isinstance(value, list):
        return "\n".join(filter(None, (_coerce_message_text(item) for item in value)))
    return str(value).strip()


def _emit_node_event(
    event: str,
    payload: dict[str, Any],
    *,
    config: RunnableConfig | None = None,
) -> None:
    metadata = config.get("metadata") if isinstance(config, dict) else None
    event_writer = metadata.get("event_writer") if isinstance(metadata, dict) else None
    emit = getattr(event_writer, "emit", None)
    if callable(emit):
        emit(event, payload)
        return
    try:
        writer = get_stream_writer()
        writer({"kind": "progress", "name": event, "data": payload})
    except RuntimeError:
        # Nodes remain directly invokable in unit tests and maintenance tasks.
        return


def _runtime_context(config: RunnableConfig | None) -> dict[str, Any]:
    configurable = config.get("configurable") if isinstance(config, dict) else None
    if not isinstance(configurable, dict):
        return {}
    keys = (
        "user_id",
        "agent_id",
        "session_id",
        "conversation_id",
        "execution_id",
        "thread_id",
    )
    return {key: configurable[key] for key in keys if configurable.get(key) is not None}
