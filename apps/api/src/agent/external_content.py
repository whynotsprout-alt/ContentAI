from __future__ import annotations

import html
import re
import unicodedata
from typing import Any

_HTML_FRAGMENT_RE = re.compile(r"<[^>]{1,500}>")
_INSTRUCTION_INJECTION_PATTERNS = (
    re.compile(
        r"\b(?:ignore|override|disregard)\b.{0,80}\b(?:instruction|prompt|system)\b",
        re.I,
    ),
    re.compile(
        r"\b(?:system prompt|developer message|tool call|call a tool|function call)\b",
        re.I,
    ),
    re.compile(r"\b(?:api[_ -]?key|password|credential|access token|secret)\b", re.I),
    re.compile(
        r"(?:\u5ffd\u7565|\u8986\u76d6|\u65e0\u89c6).{0,40}"
        r"(?:\u6307\u4ee4|\u63d0\u793a\u8bcd|\u7cfb\u7edf\u6d88\u606f)"
    ),
    re.compile(
        r"(?:\u7cfb\u7edf\u63d0\u793a\u8bcd|\u5f00\u53d1\u8005\u6d88\u606f|"
        r"\u8c03\u7528.{0,12}\u5de5\u5177|\u51fd\u6570\u8c03\u7528|"
        r"\u5bc6\u94a5|\u51ed\u636e|\u8bbf\u95ee\u4ee4\u724c)"
    ),
)


def sanitize_external_text(value: Any, *, max_chars: int) -> str:
    text = html.unescape(str(value or ""))
    text = _HTML_FRAGMENT_RE.sub(" ", text)
    text = "".join(
        char
        for char in text
        if char in "\n\t" or unicodedata.category(char) not in {"Cc", "Cf"}
    )
    return " ".join(text.split()).strip()[:max_chars]


def looks_like_instruction_injection(value: str) -> bool:
    return any(pattern.search(value) for pattern in _INSTRUCTION_INJECTION_PATTERNS)


__all__ = ["looks_like_instruction_injection", "sanitize_external_text"]
