from __future__ import annotations

from functools import lru_cache
from importlib.resources import files
from pathlib import PurePosixPath


@lru_cache
def load_prompt(name: str) -> str:
    relative = PurePosixPath(name)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError(f"Invalid prompt resource path: {name}")
    resource = files("contentai.agent.prompts").joinpath(*relative.parts)
    if not resource.is_file():
        raise FileNotFoundError(f"Prompt resource does not exist: {name}")
    content = resource.read_text(encoding="utf-8").strip()
    if not content:
        raise RuntimeError(f"Prompt resource is empty: {name}")
    return content


def load_tool_description(name: str) -> str:
    return load_prompt(f"tools/{name}.md")
