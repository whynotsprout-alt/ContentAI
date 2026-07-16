import pytest
from db.session import get_engine
from memory import LongTermMemory, MemoryRepository, extract_memory_candidates
from memory.long_term import is_transient_task_memory
from models.agent import AgentProfile
from models.enums import MemorySourceType
from sqlmodel import Session


def test_extract_memory_candidates_recognizes_user_facts():
    facts = extract_memory_candidates(
        "name: Alice\noccupation: product manager\nregion: Shanghai\n"
        "goal: finance topics\npreference: concise replies"
    )
    assert any(kind == "semantic" and content.endswith("Alice") for kind, content in facts)
    assert any(content.endswith("product manager") for _, content in facts)
    assert any(content.endswith("Shanghai") for _, content in facts)
    assert any(content.endswith("finance topics") for _, content in facts)
    assert any(content.endswith("concise replies") for _, content in facts)


def test_long_term_memory_persists_and_recalls():
    with Session(get_engine()) as session:
        memory = LongTermMemory(MemoryRepository(session))
        entry = memory.remember(
            "default-agent",
            "prefers concise replies",
            user_id="local-user",
            kind="preference",
            confidence=0.9,
            importance_score=0.7,
            source_type=MemorySourceType.manual,
        )
        recalled = memory.recall(
            "default-agent",
            "concise",
            user_id="local-user",
            limit=3,
        )

    assert entry.user_id == "local-user"
    assert entry.agent_id == "default-agent"
    assert entry.memory_scope == "long_term"
    assert entry.confidence == 0.9
    assert [item.content for item in recalled] == ["prefers concise replies"]


def test_long_term_memory_isolated_by_content_account():
    with Session(get_engine()) as session:
        session.add(
            AgentProfile(
                id="secondary-agent",
                user_id="local-user",
                name="Secondary Agent",
            )
        )
        session.commit()
        memory = LongTermMemory(MemoryRepository(session))
        memory.remember(
            "default-agent",
            "default account preference",
            user_id="local-user",
            kind="preference",
        )
        memory.remember(
            "secondary-agent",
            "secondary account preference",
            user_id="local-user",
            kind="preference",
        )

        primary = memory.recall(
            "default-agent", "preference", user_id="local-user", limit=10
        )
        secondary = memory.recall(
            "secondary-agent", "preference", user_id="local-user", limit=10
        )

    assert [item.content for item in primary] == ["default account preference"]
    assert [item.content for item in secondary] == ["secondary account preference"]


def test_long_term_memory_rejects_sensitive_credentials():
    with Session(get_engine()) as session:
        memory = LongTermMemory(MemoryRepository(session))
        with pytest.raises(ValueError, match="sensitive credentials"):
            memory.remember(
                "default-agent",
                "api_key: sk-1234567890abcdef",
                user_id="local-user",
                kind="credential",
            )


def test_long_term_memory_rejects_prior_turn_task_progress():
    transient = "接着上一条，我们已经把果切那篇完整口播稿出出来了，下一步继续选题。"
    assert is_transient_task_memory(transient)
    with Session(get_engine()) as session:
        memory = LongTermMemory(MemoryRepository(session))
        with pytest.raises(ValueError, match="transient task progress"):
            memory.remember(
                "default-agent",
                transient,
                user_id="local-user",
                source_type=MemorySourceType.turn_summary,
            )
