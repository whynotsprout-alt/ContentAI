from pathlib import Path

from core.paths import PROJECT_ROOT

MOJIBAKE_TOKENS = (
    "鎵",
    "妯",
    "濮",
    "榛",
    "鑾",
    "閫",
    "娴",
    "鐢",
)

CHECKED_PATHS = (
    "backend/src/agent/runtime/executor.py",
    "backend/src/agent/memory/retriever.py",
    "backend/src/core/hotspot_sources.py",
    "backend/src/agent/prompts/registry.py",
    "backend/src/agent/tools/search.py",
    "backend/src/agent/tools/hotspots.py",
)


def test_key_chinese_sources_do_not_contain_mojibake_tokens():
    for relative_path in CHECKED_PATHS:
        text = Path(PROJECT_ROOT / relative_path).read_text(encoding="utf-8")
        assert not any(token in text for token in MOJIBAKE_TOKENS), relative_path
