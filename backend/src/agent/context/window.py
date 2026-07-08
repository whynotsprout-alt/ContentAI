from __future__ import annotations

import re

from langchain_core.messages import BaseMessage


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
) -> list[BaseMessage]:
    if limit <= 0:
        return []
    if not messages:
        return []
    if len(messages) <= limit:
        return messages
    if not focus_message:
        return messages[-limit:]

    focus_tokens = _split_focus_tokens(focus_message)
    if not focus_tokens:
        return messages[-limit:]

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
    selected = [messages[index] for index in selected_indexes if 0 <= index < len(messages)]
    return selected[-limit:]
