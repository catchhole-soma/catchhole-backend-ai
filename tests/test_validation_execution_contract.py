"""Java-owned golden wire (grid v4, projection 2, configuration v2) and exact query identity; no provider or DB."""
import asyncio
import json
from copy import deepcopy
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.validation.client import ValidationClaim
from app.validation.contracts import (
    IncompleteInspection,
    QuestionBatch,
    QuestionDraft,
    ValidationDecision,
)
from app.validation.execution import claim_budgets, claim_roles
from app.worker.episode_validation_worker import EpisodeValidationWorker


def fixture():
    return json.loads((Path(__file__).parents[1] / "docs/contracts/validation-execution-v1.json").read_text())["execution"]


def test_claim_carries_projection_two_configuration_two_and_per_role_runtimes():
    wire = fixture()
    assert wire["projectionVersion"] == 2
    claim = ValidationClaim.from_wire(wire["responses"]["claim"])
    assert "clueVersion" not in claim.wire and claim.wire["configurationVersion"] == 2
    budgets = claim_budgets(claim)
    assert budgets["SETTING"].model_calls == 20 and budgets["SETTING"].search_limit == 10
    roles = claim_roles(claim)
    assert set(roles) == {"PLANNER", "JUDGE", "TYPO"}
    assert (roles["PLANNER"].model, roles["PLANNER"].call_options.reasoning_effort) == ("gpt-6.1-terra", "medium")
    assert roles["TYPO"].call_options.reasoning_effort == "low" and roles["JUDGE"].max_output_tokens == 100
    assert {role: {"model": runtime.model, "reasoningEffort": runtime.call_options.reasoning_effort}
            for role, runtime in roles.items()} == wire["models"]
    for sample in wire["queryEmbeddingIdentity"]["vectors"]:
        assert sha256(sample["query"].encode()).hexdigest() == sample["sha256"]


def test_old_projection_version_is_rejected_before_any_work():
    old = deepcopy(fixture()["responses"]["claim"]) | {"projectionVersion": 1, "clueVersion": 1}
    with pytest.raises(IncompleteInspection, match="UNSUPPORTED_EXECUTION_CONTRACT"):
        ValidationClaim.from_wire(old)


def claim_with(**change):
    wire = deepcopy(fixture()["responses"]["claim"])
    for key in change.get("remove", ()):
        wire.pop(key)
    if "removeRole" in change:
        wire["models"].pop(change["removeRole"])
        wire["budgets"]["roles"].pop(change["removeRole"])
    if "removeRoleKey" in change:
        role, key = change["removeRoleKey"]
        wire["budgets"]["roles"][role].pop(key)
    if "setModel" in change:
        role, model = change["setModel"]
        wire["models"][role] = model
    return ValidationClaim.from_wire(wire)


@pytest.mark.parametrize("case", fixture()["configurationV2"]["invalidClaims"], ids=lambda case: case["meaning"])
def test_legacy_or_incomplete_configuration_is_a_budget_configuration_error(case):
    claim = claim_with(**{key: value for key, value in case.items() if key != "meaning"})
    for read in (claim_budgets, claim_roles):
        with pytest.raises(IncompleteInspection, match="EXECUTION_BUDGET_CONFIGURATION_INVALID"):
            read(claim)


def test_configuration_v2_example_builds_the_production_runtimes():
    configuration = fixture()["configurationV2"]["configuration"]
    claim = SimpleNamespace(wire={"configurationVersion": configuration["configurationVersion"],
                                  "models": configuration["models"], "budgets": configuration["budgets"]})
    roles = claim_roles(claim)
    assert [roles[role].max_output_tokens for role in ("PLANNER", "JUDGE", "TYPO")] == [50000, 20000, 12000]
    budgets = claim_budgets(claim)
    assert (budgets["SETTING"].segment_chars, budgets["SETTING"].context_chars) == (10000, 1000)
    assert budgets["SETTING"].input_tokens == 80000 and budgets["TYPO"].segment_chars == 2000


def test_stage_payload_examples_have_the_grid_v4_shapes():
    wire = fixture()
    planner = wire["stagePayloads"]["PLANNER"]
    QuestionBatch.model_validate(planner["payload"])
    plan = wire["requests"]["plan"]["payload"]
    assert set(plan) == {"questions", "segments"}
    QuestionBatch.model_validate(plan)
    for row in wire["requests"]["mergedPlan"]["payload"]["questions"]:
        QuestionDraft.model_validate(row["state"]["draft"])
        assert row["state"]["lookup_obligations"] == []
        assert all(origin.startswith("PLANNER:TSEG") for origin in row["originalRefs"])
    unit = wire["requests"]["settingUnit"]["payload"]
    bundle = unit["evidence_bundle"]
    ValidationDecision.model_validate({key: value for key, value in unit.items() if key != "evidence_bundle"})
    assert sorted(bundle) == sorted(wire["judgeFlow"]["bundleKeys"])
    digest = sha256(json.dumps({key: value for key, value in bundle.items() if key != "input_digest"},
                               ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    assert digest == bundle["input_digest"] == wire["workerBundleDigestVector"]["sha256"]
    assert wire["serverDigestVectors"][2]["sha256"] == wire["requests"]["settingUnit"]["checkInputDigest"]
    for removed in ("bookSourceRead", "clueAuthority", "progressiveSourceGrid", "semanticCheckpoint"):
        assert removed not in wire


def test_data_wire_has_subjects_and_settings_and_no_clues():
    wire = fixture()
    assert list(wire["dataRequest"])[-1] == "subjectRefs" and wire["dataRequest"]["subjectRefs"] is None
    for name in ("dataInput", "dataReadTarget", "dataSubjects", "dataSettings"):
        assert wire["responses"][name]["projectionVersion"] == 2 and "clueVersion" not in wire["responses"][name]
    assert "dataEntries" not in wire["responses"]  # #381: the ENTRIES operation is gone
    assert "modelClues" not in wire["responses"]["dataReadTarget"]
    assert {"subjects", "subjectCount", "entryCount"} <= set(wire["responses"]["dataSubjects"])
    assert {"settings", "authorCorrectionProofs"} <= set(wire["responses"]["dataSettings"])


def test_judge_flow_constants_match_the_worker():
    from app.validation import contracts, judging, schemas

    flow = fixture()["judgeFlow"]
    assert flow["maxCallsPerQuestion"] == judging.MAX_JUDGE_CALLS == 3
    assert flow["concurrency"] == judging.JUDGE_CONCURRENCY == 4
    assert flow["targetContextChars"] == judging.JUDGE_TARGET_CONTEXT_CHARS == 300
    assert flow["maxSearchQueries"] == contracts.MAX_SEARCH_QUERIES == 3
    assert flow["searchQueryCodePoints"] == [1, contracts.SEARCH_QUERY_LIMIT]
    assert flow["workerChangeSearchPurpose"] == judging.WORKER_CHANGE_SEARCH_PURPOSE
    assert flow["schemas"] == {"PLANNER": schemas.PLANNER_SCHEMA_NAME, "JUDGE_OPEN": schemas.JUDGE_OPEN_SCHEMA_NAME,
                               "JUDGE_FINAL": schemas.JUDGE_FINAL_SCHEMA_NAME}
    assert flow["intentOperations"] == list(contracts.LookupIntent.model_fields["operation"].annotation.__args__)


def test_runtime_is_disabled_and_cli_refuses_missing_explicit_opt_in():
    with pytest.raises(ValueError, match="disabled"):
        EpisodeValidationWorker(None, None)
    from scripts.run_validation_worker import parser, run
    args = parser().parse_args(["--backend-url", "https://never-called.invalid", "--worker-id", "fixture"])
    with pytest.raises(ValueError, match="OFF"):
        asyncio.run(run(args))
