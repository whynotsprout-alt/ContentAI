from types import SimpleNamespace

import agent.workflows.final_evidence as final_evidence
import pytest
from agent.workflows.deep_research import ContentEvidenceInvalidError
from agent.workflows.final_evidence import (
    build_research_final_proof,
    build_supported_research_evidence,
    select_research_evidence,
    validate_research_final_presentation,
    validate_research_final_selection,
)


def _package() -> SimpleNamespace:
    return SimpleNamespace(
        id="rsp-evidence",
        topic="Evidence-backed topic",
        topic_hash="topic-hash",
        package_data={
            "core_conclusion": {
                "text": "The durable conclusion.",
                "source_ids": ["S2", "S1"],
            },
            "findings": [
                {
                    "claim": "The durable finding.",
                    "evidence": "Evidence detail.",
                    "source_ids": ["S1"],
                }
            ],
        },
        sources=[
            {"source_id": "S1", "url": "https://one.example/source", "isolated": False},
            {"source_id": "S2", "url": "https://two.example/source", "isolated": False},
        ],
    )


def test_research_final_selection_is_only_stable_durable_claim_ids():
    evidence = build_supported_research_evidence(_package())
    first_id, second_id = [claim["claim_id"] for claim in evidence["claims"]]

    selection = validate_research_final_selection(
        {"claim_ids": [second_id, first_id]}, evidence=evidence
    )

    assert selection.claim_ids == [second_id, first_id]
    assert all(set(claim) >= {"claim_id", "claim", "source_ids"} for claim in evidence["claims"])
    assert first_id.startswith("clm_")
    assert second_id.startswith("clm_")


def test_research_final_selection_protocol_has_no_model_authored_answer_envelope():
    assert set(final_evidence.ResearchFinalSelection.model_fields) == {"claim_ids"}
    assert set(final_evidence.ResearchFinalPresentation.model_fields) == {"content"}


@pytest.mark.parametrize(
    "selection",
    [
        {"claim_ids": []},
        {"claim_ids": ["clm_unknown"]},
        {"claim_ids": ["clm_unknown"], "answer": "model-authored answer"},
        {"claim_ids": ["clm_unknown"], "claims": [{"text": "forged", "source_ids": ["S1"]}]},
    ],
)
def test_research_final_selection_rejects_empty_unknown_or_model_authored_content(selection):
    evidence = build_supported_research_evidence(_package())

    with pytest.raises(ContentEvidenceInvalidError):
        validate_research_final_selection(selection, evidence=evidence)


def test_research_final_presentation_and_proof_are_bound_to_selected_durable_evidence():
    evidence = build_supported_research_evidence(_package())
    claim_id = evidence["claims"][0]["claim_id"]
    selection = validate_research_final_selection({"claim_ids": [claim_id]}, evidence=evidence)
    selected_evidence = select_research_evidence(evidence, selection.claim_ids)
    answer = (
        "## 核心结论\n\n这是一段基于已核验资料的用户友好说明。\n\n"
        "## 来源\n\n[来源一](https://one.example/source)\n"
        "[来源二](https://two.example/source)"
    )
    presentation = validate_research_final_presentation(
        {"content": answer}, selected_evidence=selected_evidence
    )
    proof = build_research_final_proof(evidence, selection.claim_ids, presentation.content)

    assert presentation.content == answer
    assert proof == {
        "package_id": "rsp-evidence",
        "topic_hash": "topic-hash",
        "claim_ids": [claim_id],
        "digest": proof["digest"],
    }
    assert len(proof["digest"]) == 64


@pytest.mark.parametrize(
    "content",
    [
        "## 结论\n\n内容足够长，但没有来源链接，不能作为最终答案。",
        "## 结论\n\n不要泄露 S1。\n\n[来源](https://one.example/source)\n[来源](https://two.example/source)",
        "## 结论\n\n使用了未选择的来源。\n\n[来源](https://invalid.example/source)",
    ],
)
def test_research_final_presentation_rejects_missing_raw_or_unknown_citations(content):
    evidence = build_supported_research_evidence(_package())
    selected = select_research_evidence(evidence, [evidence["claims"][0]["claim_id"]])

    with pytest.raises(ContentEvidenceInvalidError):
        validate_research_final_presentation({"content": content}, selected_evidence=selected)
