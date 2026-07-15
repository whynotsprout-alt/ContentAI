import pytest
from db.session import get_engine
from memory import LongTermMemory, MemoryRepository, extract_memory_candidates
from memory.long_term import is_transient_task_memory
from models.enums import MemoryOwnerType, MemorySourceType
from sqlmodel import Session


def test_extract_memory_candidates_recognizes_user_facts():
    facts = extract_memory_candidates(
        "name: Alice\n"
        "occupation: product manager\n"
        "region: Shanghai\n"
        "goal: finance topics\n"
        "preference: concise replies"
    )

    assert any(kind == "semantic" and content.endswith("Alice") for kind, content in facts)
    assert any(
        kind == "semantic" and content.endswith("product manager") for kind, content in facts
    )
    assert any(kind == "semantic" and content.endswith("Shanghai") for kind, content in facts)
    assert any(kind == "semantic" and content.endswith("finance topics") for kind, content in facts)
    assert any(
        kind == "semantic" and content.endswith("concise replies") for kind, content in facts
    )


def test_long_term_memory_persists_and_recalls():
    with Session(get_engine()) as session:
        memory = LongTermMemory(MemoryRepository(session))
        entry = memory.remember(
            "memory-test-agent",
            "prefers concise replies",
            tenant_id="tenant_a",
            user_id="user_1",
            kind="preference",
            confidence=0.9,
            importance_score=0.7,
            source_type=MemorySourceType.manual,
        )
        recalled = memory.recall(
            "memory-test-agent",
            "concise",
            tenant_id="tenant_a",
            user_id="user_1",
            limit=3,
        )

    assert entry.key
    assert entry.tenant_id == "tenant_a"
    assert entry.user_id == "user_1"
    assert entry.owner_type == MemoryOwnerType.agent
    assert entry.confidence == 0.9
    assert entry.importance_score == 0.7
    assert entry.source_type == MemorySourceType.manual
    assert [item.content for item in recalled] == ["prefers concise replies"]


def test_long_term_memory_isolated_by_tenant_user_and_account():
    with Session(get_engine()) as session:
        memory = LongTermMemory(MemoryRepository(session))
        memory.remember(
            "account_a",
            "tenant A account A preference",
            tenant_id="tenant_a",
            user_id="user_1",
            kind="preference",
            source_type=MemorySourceType.manual,
        )
        memory.remember(
            "account_b",
            "tenant A account B preference",
            tenant_id="tenant_a",
            user_id="user_1",
            kind="preference",
            source_type=MemorySourceType.manual,
        )
        memory.remember(
            "account_a",
            "tenant B account A preference",
            tenant_id="tenant_b",
            user_id="user_1",
            kind="preference",
            source_type=MemorySourceType.manual,
        )

        account_a = memory.recall(
            "account_a",
            "preference",
            tenant_id="tenant_a",
            user_id="user_1",
            limit=10,
        )
        account_b = memory.recall(
            "account_b",
            "preference",
            tenant_id="tenant_a",
            user_id="user_1",
            limit=10,
        )
        tenant_b = memory.recall(
            "account_a",
            "preference",
            tenant_id="tenant_b",
            user_id="user_1",
            limit=10,
        )

    assert [item.content for item in account_a] == ["tenant A account A preference"]
    assert [item.content for item in account_b] == ["tenant A account B preference"]
    assert [item.content for item in tenant_b] == ["tenant B account A preference"]


def test_long_term_memory_rejects_sensitive_credentials():
    with Session(get_engine()) as session:
        memory = LongTermMemory(MemoryRepository(session))
        with pytest.raises(ValueError, match="sensitive credentials"):
            memory.remember(
                "account_a",
                "api_key: sk-1234567890abcdef",
                tenant_id="tenant_a",
                user_id="user_1",
                kind="credential",
                source_type=MemorySourceType.manual,
            )


def test_long_term_memory_rejects_prior_turn_task_progress():
    transient = "接着上一条，我们已经把果切那篇完整口播稿出出来了，下一步继续选题。"
    assert is_transient_task_memory(transient)

    with Session(get_engine()) as session:
        memory = LongTermMemory(MemoryRepository(session))
        with pytest.raises(ValueError, match="transient task progress"):
            memory.remember(
                "account_a",
                transient,
                tenant_id="tenant_a",
                user_id="user_1",
                kind="semantic",
                source_type=MemorySourceType.turn_summary,
            )
