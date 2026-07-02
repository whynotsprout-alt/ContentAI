from agent.memory import LongTermMemory, MemoryRepository
from agent.memory.retriever import extract_memory_candidates
from db.session import engine
from langgraph.store.memory import InMemoryStore
from sqlmodel import Session


def test_extract_memory_candidates_recognizes_user_facts():
    facts = extract_memory_candidates(
        "我叫李雷，职业是产品经理，来自上海，目标是做财经选题，偏好短句。"
    )

    assert ("profile", "姓名：李雷") in facts
    assert ("profile", "职业：产品经理") in facts
    assert ("profile", "地区：上海") in facts
    assert ("goal", "目标：做财经选题") in facts
    assert ("preference", "偏好：短句") in facts


def test_long_term_memory_persists_and_recalls():
    with Session(engine) as session:
        memory = LongTermMemory(MemoryRepository(session), InMemoryStore())
        entry = memory.remember("memory-test-agent", "偏好：回答要短句", kind="preference")
        recalled = memory.recall("memory-test-agent", "短句", limit=3)

    assert entry.key
    assert [item.content for item in recalled] == ["偏好：回答要短句"]
