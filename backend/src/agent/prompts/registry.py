from __future__ import annotations

from functools import lru_cache
from pathlib import Path

PROMPT_DIR = Path(__file__).resolve().parent


@lru_cache
def load_prompt(name: str) -> str:
    path = PROMPT_DIR / name
    if not path.exists():
        raise FileNotFoundError(f"Prompt file does not exist: {name}")
    content = path.read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError(f"Prompt file is empty: {name}")
    return content


def build_system_prompt(
    *,
    account_name: str,
    account_description: str,
    account_instructions: str,
    short_term_summary: str,
    long_term_memory: str,
    tool_names: list[str],
) -> str:
    tool_list = "\n".join(f"- {name}" for name in tool_names) if tool_names else "- 暂无"
    return load_prompt("system.md").format(
        account_name=account_name or "默认账号",
        account_description=account_description or "无",
        account_instructions=account_instructions or "无",
        short_term_summary=short_term_summary or "无",
        long_term_memory=long_term_memory or "无",
        tool_list=tool_list,
    )
