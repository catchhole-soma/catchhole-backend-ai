import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchCompleteRequest,
    WorkerWorldSettingComparisonBatchPayload,
)
from evals.world_setting_comparison.models import WorldSettingComparisonRuntimeResult
from evals.world_setting_comparison.runtime_adapter import (
    adapt_batch_completion_for_evaluation,
)

FIXTURE_PATH = (
    Path(__file__).parent
    / "fixtures"
    / "world_setting_comparison_runtime"
    / "merged_existing_target.json"
)


def test_runtime_adapter_preserves_source_membership_and_canonical_decision() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    batch = WorkerWorldSettingComparisonBatchPayload.model_validate(fixture["batch"])
    completion = WorkerWorldSettingComparisonBatchCompleteRequest.model_validate(
        fixture["completion"]
    )
    expected = WorldSettingComparisonRuntimeResult.model_validate(fixture["expectedRuntimeResult"])

    result = adapt_batch_completion_for_evaluation(batch, completion)

    assert result == expected
    assert result.model_dump(mode="json", by_alias=True) == fixture["expectedRuntimeResult"]
    assert [source.candidate_id for source in result.decisions[0].source_candidates] == [
        batch.candidates[0].candidate_id,
        batch.candidates[1].candidate_id,
    ]
    assert result.decisions[0].matched_property_name == "무기"
    assert result.canonical_subject_key == batch.canonical_subject_key
    assert result.decisions[0].operation == "MERGE"
    assert result.decisions[0].consolidation_status == "MERGED"


def test_runtime_adapter_preserves_existing_root_property_moves() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture["completion"]["decisions"][0]["existingRootPropertyNamesToMove"] = [
        "기존 생명력"
    ]
    batch = WorkerWorldSettingComparisonBatchPayload.model_validate(fixture["batch"])
    completion = WorkerWorldSettingComparisonBatchCompleteRequest.model_validate(
        fixture["completion"]
    )

    result = adapt_batch_completion_for_evaluation(batch, completion)

    assert result.decisions[0].existing_root_property_names_to_move == ["기존 생명력"]
    assert result.model_dump(mode="json", by_alias=True)["decisions"][0][
        "existingRootPropertyNamesToMove"
    ] == ["기존 생명력"]


@pytest.mark.parametrize(
    ("source_refs", "message"),
    [
        (["C1"], "omits candidate refs"),
        (["C1", "C1"], "reuses candidate refs"),
        (["C1", "C3"], "unknown candidate refs"),
    ],
)
def test_runtime_adapter_requires_exact_source_coverage(
    source_refs: list[str],
    message: str,
) -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture["completion"]["decisions"][0]["sourceCandidateRefs"] = source_refs
    batch = WorkerWorldSettingComparisonBatchPayload.model_validate(fixture["batch"])
    completion = WorkerWorldSettingComparisonBatchCompleteRequest.model_validate(
        fixture["completion"]
    )

    with pytest.raises(ValueError, match=message):
        adapt_batch_completion_for_evaluation(batch, completion)


def test_runtime_result_schema_rejects_unregistered_fields() -> None:
    fixture = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
    fixture["expectedRuntimeResult"]["unexpectedMetric"] = 1

    with pytest.raises(ValidationError, match="extra_forbidden"):
        WorldSettingComparisonRuntimeResult.model_validate(fixture["expectedRuntimeResult"])
