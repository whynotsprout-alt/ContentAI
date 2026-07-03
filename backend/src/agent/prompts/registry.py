from __future__ import annotations

from functools import lru_cache

from core.paths import PROMPTS_DIR

PROMPT_DIR = PROMPTS_DIR


@lru_cache
def load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file does not exist: {name}")
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError(f"Prompt file is empty: {name}")
    return content


def load_tool_description(name: str) -> str:
    return load_prompt(f"tools/{name}.md")


def build_system_prompt(
    *,
    account_id: str,
    account_name: str,
    account_positioning: str,
    topic_scoring_prompt: str,
    content_creation_prompt: str,
    allowed_hotspot_sources: str,
    short_term_summary: str,
    long_term_memory: str,
    tool_names: list[str],
) -> str:
    tool_list = "\n".join(f"- {name}" for name in tool_names) if tool_names else "- 暂无"
    return load_prompt("system.md").format(
        account_id=account_id,
        account_name=account_name or "默认账号",
        account_positioning=account_positioning,
        topic_scoring_prompt=topic_scoring_prompt,
        content_creation_prompt=content_creation_prompt,
        allowed_hotspot_sources=allowed_hotspot_sources,
        short_term_summary=short_term_summary or "无",
        long_term_memory=long_term_memory or "无",
        tool_list=tool_list,
    )
