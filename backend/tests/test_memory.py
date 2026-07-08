from memory import LongTermMemory, MemoryRepository
from memory import extract_memory_candidates
from agent.runtime.checkpoint import build_store
from db.session import get_engine
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
        kind == "semantic" and content.endswith("product manager")
        for kind, content in facts
    )
    assert any(kind == "semantic" and content.endswith("Shanghai") for kind, content in facts)
    assert any(kind == "semantic" and content.endswith("finance topics") for kind, content in facts)
    assert any(
        kind == "semantic" and content.endswith("concise replies")
        for kind, content in facts
    )


def test_long_term_memory_persists_and_recalls():
    with Session(get_engine()) as session:
        memory = LongTermMemory(MemoryRepository(session), build_store())
        entry = memory.remember(
            "memory-test-agent",
            "prefers concise replies",
            tenant_id="tenant_a",
            user_id="user_1",
            kind="preference",
            confidence=0.9,
            importance_score=0.7,
            source_type="test",
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
    assert entry.confidence == 0.9
    assert entry.importance_score == 0.7
    assert entry.source_type == "test"
    assert [item.content for item in recalled] == ["prefers concise replies"]
