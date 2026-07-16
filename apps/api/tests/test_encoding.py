from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[3]

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
    "apps/api/src",
    "apps/api/tests",
)


def test_key_chinese_sources_do_not_contain_mojibake_tokens():
    for relative_path in CHECKED_PATHS:
        path = PROJECT_ROOT / relative_path
        files = path.rglob("*") if path.is_dir() else [path]
        for file_path in files:
            if file_path.name == "test_encoding.py":
                continue
            if file_path.suffix not in {".py", ".md"}:
                continue
            if any(part.startswith(".") for part in file_path.relative_to(PROJECT_ROOT).parts):
                continue
            text = file_path.read_text(encoding="utf-8")
            assert not any(token in text for token in MOJIBAKE_TOKENS), str(
                file_path.relative_to(PROJECT_ROOT)
            )
