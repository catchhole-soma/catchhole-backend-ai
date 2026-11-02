"""Java-owned golden wire and exact query identity; no provider or database access."""
import asyncio
import json
from hashlib import sha256
from pathlib import Path

import pytest

from app.validation.checkpoint import SemanticState
from app.validation.client import ValidationClaim
from app.validation.contracts import IncompleteInspection
from app.validation.execution import claim_budgets
from app.worker.episode_validation_worker import EpisodeValidationWorker


def fixture():
    return json.loads((Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json").read_text())["execution"]


def test_exact_common_claim_budget_and_typed_checkpoint():
    wire = fixture()
    claim = ValidationClaim.from_wire(wire["responses"]["claim"])
    assert claim_budgets(claim)["SETTING"].model_calls == 20
    state = SemanticState.model_validate(wire["requests"]["checkpoint"]["payload"])
    assert state.inputDigest == claim.scope.inputDigest
    for sample in wire["queryEmbeddingIdentity"]["vectors"]:
        assert sha256(sample["query"].encode()).hexdigest() == sample["sha256"]
    claim.wire["budgets"]["roles"].pop("COVERAGE")
    with pytest.raises(IncompleteInspection, match="EXECUTION_BUDGET_CONFIGURATION_INVALID"):
        claim_budgets(claim)


def test_runtime_is_disabled_and_cli_refuses_missing_explicit_opt_in():
    with pytest.raises(ValueError, match="disabled"):
        EpisodeValidationWorker(None, None)
    from scripts.run_validation_worker import parser, run
    args = parser().parse_args(["--backend-url", "https://never-called.invalid", "--worker-id", "fixture"])
    with pytest.raises(ValueError, match="OFF"):
        asyncio.run(run(args))


def test_grid_v3_cases_match_local_subject_matching_and_stage_order():
    from app.validation.contracts import SubjectResolution, split_segments
    from app.validation.service import _settings, grid_segment_subjects, mechanical_subject_refs

    cases = [case for case in fixture()["gridCases"] if "expectedSetting" in case]
    assert any(any(row["subjectRefs"] for row in case["expectedSetting"]["segmentSubjects"]) for case in cases)
    # GH349 amendment: the SETTING grid is v3 (v2 shape, 1-character names excluded from mechanical matching).
    assert fixture()["progressiveSourceGrid"]["gridVersion"] == 3
    for case in cases:
        grid, budget = case["expectedSetting"], case["configuration"]["budgets"]["setting"]
        assert grid["gridVersion"] == 3
        settings = _settings([{"entryRef": f"B{n + 1}", **row} for n, row in enumerate(case.get("baselineEntries", []))])
        segments = split_segments(case["target"], core_chars=budget["segmentChars"],
                                  context_chars=budget["contextChars"])
        subjects = {row["subject_ref"] for row in settings}
        assert grid_segment_subjects(grid, segments, subjects) == mechanical_subject_refs(settings, case["target"], segments)
        target_units = [(row["stage"], row["ref"]) for row in grid["units"]
                        if row["stage"] in {"PLANNER", "SUBJECT_RESOLUTION", "COVERAGE", "RELATED_SELECTION"}]
        assert target_units == [(stage, segment.ref) for segment in segments
                                for stage in ("PLANNER", "SUBJECT_RESOLUTION", "COVERAGE")]
        assert case["expectedTypo"]["gridVersion"] == 1
    example = fixture()["stagePayloads"]["SUBJECT_RESOLUTION"]
    assert (example["stage"], example["ref"]) == ("SUBJECT_RESOLUTION", "TSEG1")
    SubjectResolution.model_validate(example["payload"])
