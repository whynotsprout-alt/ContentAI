from __future__ import annotations

import json
import re
from collections.abc import Callable
from typing import Any

from contentai.services.errors import CurrentInputTooLargeError
from langchain_core.messages import BaseMessage, message_to_dict

CHARS_PER_TOKEN_ESTIMATE = 4


class TokenCounter:
    def __init__(
        self,
        *,
        provider_count: Callable[[list[BaseMessage]], int] | None = None,
        tools: list[Any] | None = None,
    ) -> None:
        self._provider_count = provider_count
        self._tool_schemas = tuple(_canonical_tool_schema(tool) for tool in (tools or []))
        self._tool_schema_tokens = _tool_schema_tokens_from_schemas(self._tool_schemas)

    def count_messages(self, messages: list[BaseMessage]) -> int:
        if self._provider_count is not None:
            try:
                count = int(self._provider_count(messages))
                if count > 0:
                    return count + self._tool_schema_tokens
            except Exception:  # noqa: BLE001
                self._provider_count = None
        payload = {
            "messages": [message_to_dict(message) for message in messages],
            "tools": list(self._tool_schemas),
        }
        encoded = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
        return max(1, len(encoded))

def _tool_schema_tokens_from_schemas(schemas: tuple[dict[str, Any], ...]) -> int:
    if not schemas:
        return 0
    encoded = json.dumps(
        list(schemas),
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return (len(encoded) + CHARS_PER_TOKEN_ESTIMATE - 1) // CHARS_PER_TOKEN_ESTIMATE


def _canonical_tool_schema(tool: Any) -> dict[str, Any]:
    if isinstance(tool, dict):
        return {str(key): value for key, value in tool.items()}
    schema: Any = getattr(tool, "args_schema", None)
    if schema is None:
        get_input_schema = getattr(tool, "get_input_schema", None)
        if callable(get_input_schema):
            schema = get_input_schema()
    model_json_schema = getattr(schema, "model_json_schema", None)
    if callable(model_json_schema):
        try:
            schema = model_json_schema()
        except Exception:  # noqa: BLE001
            schema = str(schema)
    return {
        "name": str(getattr(tool, "name", "") or ""),
        "description": str(getattr(tool, "description", "") or ""),
        "parameters": schema if schema is not None else {},
    }


def _normalize_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _split_focus_tokens(focus_message: str | None) -> list[str]:
    if not focus_message:
        return []
    text = _normalize_text(focus_message)
    if not text:
        return []
    tokens: list[str] = []
    for token in re.split(r"[\s,.;:!?，。！？；：、\-\+—\(\)\"“”‘’]+", text):
        token = token.strip()
        if not token or len(token) < 2:
            continue
        tokens.append(token)
    return tokens[:16]


def _message_text(message: BaseMessage) -> str:
    content = message.content
    if isinstance(content, str):
        return _normalize_text(content)
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict) and isinstance(part.get("text"), str):
                parts.append(part.get("text", ""))
        return _normalize_text(" ".join(parts))
    return _normalize_text(str(content))


def estimate_message_tokens(message: BaseMessage) -> int:
    text = _message_text(message)
    return max(1, (len(text) + CHARS_PER_TOKEN_ESTIMATE - 1) // CHARS_PER_TOKEN_ESTIMATE)


def _score_relevance(message: BaseMessage, focus_tokens: list[str]) -> int:
    if not focus_tokens:
        return 0
    text = _message_text(message)
    return sum(1 for token in focus_tokens if token in text)


def trim_context_window(
    messages: list[BaseMessage],
    *,
    limit: int,
    min_focused_retain: int,
    focus_message: str | None = None,
    max_tokens: int | None = None,
    token_counter: TokenCounter | None = None,
    fixed_messages: list[BaseMessage] | None = None,
) -> list[BaseMessage]:
    selected, _token_count = trim_context_window_with_count(
        messages,
        limit=limit,
        min_focused_retain=min_focused_retain,
        focus_message=focus_message,
        max_tokens=max_tokens,
        token_counter=token_counter,
        fixed_messages=fixed_messages,
    )
    return selected


def trim_context_window_with_count(
    messages: list[BaseMessage],
    *,
    limit: int,
    min_focused_retain: int,
    focus_message: str | None = None,
    max_tokens: int | None = None,
    token_counter: TokenCounter | None = None,
    fixed_messages: list[BaseMessage] | None = None,
) -> tuple[list[BaseMessage], int | None]:
    if limit <= 0:
        return [], None
    if not messages:
        return [], None
    token_budget = max_tokens if max_tokens is not None and max_tokens > 0 else None
    if len(messages) <= limit:
        selected = list(messages)
    elif not focus_message:
        selected = list(messages[-limit:])
    else:
        focus_tokens = _split_focus_tokens(focus_message)
        if not focus_tokens:
            selected = list(messages[-limit:])
        else:
            scored_indexes: list[tuple[int, int]] = []
            for index, message in enumerate(messages):
                score = _score_relevance(message, focus_tokens)
                if score:
                    scored_indexes.append((index, score))

            keep_count = max(min_focused_retain, limit // 2)
            recent_start = max(0, len(messages) - keep_count)
            keep_indexes = set(range(recent_start, len(messages)))
            if scored_indexes:
                scored_indexes.sort(key=lambda item: (item[1], item[0]), reverse=True)
                for index, _ in scored_indexes[: max(1, limit - keep_count)]:
                    keep_indexes.add(index)

            selected_indexes = sorted(keep_indexes)
            selected = [
                messages[index] for index in selected_indexes if 0 <= index < len(messages)
            ][-limit:]
    if token_counter is not None and token_budget is not None:
        return _trim_full_input_budget(
            selected,
            fixed_messages=fixed_messages or [],
            token_counter=token_counter,
            max_tokens=token_budget,
        )
    return _trim_to_token_budget(selected, token_budget), None


def _trim_full_input_budget(
    messages: list[BaseMessage],
    *,
    fixed_messages: list[BaseMessage],
    token_counter: TokenCounter,
    max_tokens: int,
) -> tuple[list[BaseMessage], int]:
    current = messages[-1]
    immutable = [*fixed_messages, current]
    if token_counter.count_messages(immutable) > max_tokens:
        raise CurrentInputTooLargeError("Current input exceeds the model context budget.")

    history = messages[:-1]
    full_input = [*fixed_messages, *history, current]
    full_count = token_counter.count_messages(full_input)
    if full_count <= max_tokens:
        return messages, full_count

    low = 0
    high = len(history)
    while low < high:
        midpoint = (low + high) // 2
        candidate = [*fixed_messages, *history[midpoint:], current]
        if token_counter.count_messages(candidate) <= max_tokens:
            high = midpoint
        else:
            low = midpoint + 1
    selected = [*history[low:], current]
    return selected, token_counter.count_messages([*fixed_messages, *selected])


def _trim_to_token_budget(
    messages: list[BaseMessage],
    max_tokens: int | None,
) -> list[BaseMessage]:
    if max_tokens is None:
        return messages
    if max_tokens <= 0 or not messages:
        return []

    selected_reversed: list[BaseMessage] = []
    used_tokens = 0
    for message in reversed(messages):
        message_tokens = estimate_message_tokens(message)
        if selected_reversed and used_tokens + message_tokens > max_tokens:
            continue
        selected_reversed.append(message)
        used_tokens += message_tokens
        if used_tokens >= max_tokens:
            break

    selected_reversed.reverse()
    return selected_reversed
