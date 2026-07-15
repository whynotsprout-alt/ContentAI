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
