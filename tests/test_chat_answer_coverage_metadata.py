"""Coverage corrections never manufacture claims, remove gaps or bypass schema checks."""

import copy

import pytest

from app.schemas.chat_hybrid import GroundedAnswer
from tests.test_chat_current_source_comparison import comparison_answer
from tests.test_chat_requested_parts import answer, claim


@pytest.mark.parametrize("coverage", ["COMPLETE", "PARTIAL", "NONE"])
def test_metadata_recovery_preserves_every_claim_conflict_and_support(coverage):
    original = comparison_answer()
    original["coverage"] = coverage
    expected = GroundedAnswer.model_validate(comparison_answer()).model_dump()

    result = GroundedAnswer.model_validate(original)

    assert result.model_dump() == expected
    assert original["coverage"] == coverage


def test_missing_facets_and_explanations_cannot_disappear_during_metadata_recovery():
    original = answer(claim("그는 돌아갔다.", "그는 돌아갔다.", parts=("P1",)), missing=("P2",))
    original["missingExplanations"] = [{"facet": "P2", "text": "돌아간 이유는 확인하지 못했어요."}]
    expected = GroundedAnswer.model_validate(original).model_dump()
    original["coverage"] = "COMPLETE"

    assert GroundedAnswer.model_validate(original).model_dump() == expected


def test_unsupported_complete_answer_is_empty_without_invented_content():
    result = GroundedAnswer.model_validate(answer(coverage="COMPLETE"))

    assert result.coverage == "NONE"
    assert result.claims == result.conflicts == []


def test_legacy_partial_is_not_promoted_without_request_validation():
    original = answer(claim("그는 돌아갔다.", "그는 돌아갔다."), coverage="PARTIAL")

    assert GroundedAnswer.model_validate(original).coverage == "PARTIAL"


@pytest.mark.parametrize(
    "problem", ["duplicate_refs", "duplicate_facets", "invalid_explanation", "invalid_coverage"]
)
def test_coverage_recovery_does_not_accept_other_invalid_structure(problem):
    original = copy.deepcopy(comparison_answer())
    original["coverage"] = "COMPLETE"
    if problem == "duplicate_refs":
        original["claims"][0]["rowRefs"] = ["R1", "R1"]
    elif problem == "duplicate_facets":
        original["claims"][0]["answeredFacets"] = ["P1", "P1"]
    elif problem == "invalid_explanation":
        original["missingExplanations"] = [{"facet": "P1", "text": "설명"}]
    else:
        original["coverage"] = "MAYBE"

    with pytest.raises(ValueError):
        GroundedAnswer.model_validate(original)
