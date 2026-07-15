from __future__ import annotations

import re

from memory.types import MemoryEntry

FACT_PATTERNS = (
    (re.compile(r"(?:我叫|我的名字是|姓名[:：]\s*)([^，。；;\n]+)"), "profile", "姓名"),
    (re.compile(r"(?:职业是|我是做|岗位是|职业[:：]\s*)([^，。；;\n]+)"), "profile", "职业"),
    (re.compile(r"(?:来自|在|地区[:：]\s*)([^，。；;\n]+)"), "profile", "地区"),
    (re.compile(r"(?:目标是|目标[:：]\s*)([^，。；;\n]+)"), "goal", "目标"),
    (re.compile(r"(?:偏好|喜欢|倾向于|风格[:：]\s*)([^，。；;\n]+)"), "preference", "偏好"),
)


def extract_memory_candidates(text: str) -> list[tuple[str, str]]:
    source = text.strip()
    if not source:
        return []

    candidates: list[tuple[str, str]] = []
    for pattern, kind, label in FACT_PATTERNS:
        for match in pattern.finditer(source):
            value = match.group(1).strip()
            if value:
                candidates.append((kind, f"{label}：{value}"))

    for line in source.splitlines():
        if ":" not in line and "：" not in line:
            continue
        left, _, right = line.replace("：", ":").partition(":")
        left = left.strip()
        right = right.strip()
        if left and right and len(left) <= 20:
            candidates.append(("semantic", f"{left}：{right}"))

    seen: set[str] = set()
    output: list[tuple[str, str]] = []
    for kind, content in candidates:
        key = content.casefold()
        if key in seen:
            continue
        seen.add(key)
        output.append((kind, content[:300]))
    return output[:12]


def render_memories(memories: list[MemoryEntry]) -> str:
    if not memories:
        return "无"
    return "\n".join(f"- [{memory.kind}] {memory.content}" for memory in memories)
