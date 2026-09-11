import asyncio
import json

import httpx
import pytest

from app.domain.enums import AnalysisFailureCode
from evals.multi_stage_setting.contracts import (
    CandidateProcessingRecord,
    CharacterStage1Prediction,
    CharacterStage2Prediction,
    PredictionBundleV3,
    ScenarioPrediction,
)
from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.processing import ProcessingTrace, processing_outcomes
from evals.multi_stage_setting.report_cli import (
    build_public_diagnostics,
    build_source_free_summary,
    render_markdown_summary,
)
from evals.multi_stage_setting.runtime_adapter import (
    RuntimeComponents,
    run_multi_stage_predictions,
)
from tests.test_multi_stage_setting_extra_report import (
    _bundle,
    _empty_gold,
    _world_decision,
    _world_gold,
    _world_source,
)
from tests.test_multi_stage_setting_runtime_adapter import (
    _AddCharacterComparator,
    _IncompleteWorldComparator,
    _IndependentWorldPropertiesExtractor,
)


def _character(kind="SETTING", match="MATCHED", candidate_id="C1"):
    return CharacterStage1Prediction(
        candidate_id=candidate_id,
        domain="CHARACTER",
        candidate_kind=kind,
        entity_name="파눈",
        entity_ref="character:panun" if match == "MATCHED" else None,
        match_status=match,
        **(
            {}
            if kind == "CHARACTER_DISCOVERY"
            else dict(
                fact_type="PROFILE",
                fact_key="profile.species",
                value_type="STRING",
                display_value="바바리안",
                value_json={"value": "바바리안"},
            )
        ),
    )


def _record(source, status, reason, forwarded=False, **kwargs):
    return CandidateProcessingRecord(
        candidate_id=source.candidate_id,
        domain=source.domain,
        comparison_forwarded=forwarded,
        status=status,
        reason_code=reason,
        stage="CHARACTER_STAGE2" if forwarded else "CHARACTER_HANDOFF",
        **kwargs,
    )


@pytest.mark.parametrize(
    "kind,match,status,reason,label",
    [
        (
            "CHARACTER_DISCOVERY",
            "UNRESOLVED",
            "NOT_APPLICABLE",
            "CHARACTER_DISCOVERY",
            "비교 대상 아님",
        ),
        (
            "SETTING",
            "UNRESOLVED",
            "WAITING_FOR_CHARACTER",
            "CHARACTER_UNRESOLVED",
            "인물 연결 대기",
        ),
        ("SETTING", "AMBIGUOUS", "AMBIGUOUS_CHARACTER", "CHARACTER_AMBIGUOUS", "인물 선택 필요"),
    ],
)
def test_explicit_skip_status_in_both_tables(kind, match, status, reason, label):
    source = _character(kind, match)
    gold = _empty_gold("CHARACTER")
    bundle = _bundle(gold, [source], [])
    bundle.scenarios[0] = ScenarioPrediction(
        scenario_id="S1",
        stage1=[source],
        processing_version=1,
        processing=[_record(source, status, reason)],
    )
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    public = build_public_diagnostics(report)
    assert public[0]["processing"][0]["comparisonForwarded"] is False
    assert public[0]["stage2"][0]["processing"]["status"] == status
    markdown = render_markdown_summary(report)
    assert label in markdown
    assert "파눈" in next(line for line in markdown.splitlines() if "모델 추출: C1" in line)


@pytest.mark.parametrize("mode", ["FIXED", "ROLLING", "ORACLE"])
@pytest.mark.parametrize("operation", ["ADD", "EXCLUDE"])
def test_world_batch_coverage_and_score_state_invariance(mode, operation):
    gold = _world_gold()
    ids = ["W1", "W2"] if mode == "ORACLE" else ["P1", "P2"]
    # ORACLE uses Gold row IDs, so keep one source for its score fixture.
    if mode == "ORACLE":
        ids = ["W1"]
    sources = [_world_source(i) for i in ids]
    decision = _world_decision(ids[0], operation).model_copy(update={"source_candidate_ids": ids})
    bundle = _bundle(gold, sources, [decision], mode=mode)
    baseline = asyncio.run(evaluate_multi_stage(gold, bundle))
    trace = ProcessingTrace(candidates=sources)
    trace.start(ids, "WORLD_STAGE2", forwarded=True)
    trace.complete(decision)
    bundle.scenarios[0] = ScenarioPrediction(
        scenario_id="S1",
        stage1=sources,
        stage2=[decision],
        processing_version=1,
        processing=trace.records,
    )
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    assert build_source_free_summary(baseline) == build_source_free_summary(report)
    for a, b in zip(baseline["scenarios"], report["scenarios"], strict=True):
        assert a["predictedAfterStateHash"] == b["predictedAfterStateHash"]
    rows = build_public_diagnostics(report)[0]["processing"]
    assert {r["candidateId"] for r in rows} == set(ids)
    assert all(r["operation"] == operation and r["status"] == "COMPARED" for r in rows)


@pytest.mark.parametrize(
    "corruption", ["missing", "duplicate", "unknown", "contradiction", "operation", "discovery"]
)
def test_new_processing_contract_rejects_invalid_records(corruption):
    source = _character()
    decision = CharacterStage2Prediction(
        source_candidate_id="C1",
        domain="CHARACTER",
        operation="EXCLUDE",
        resolved_canonical_fact_key="profile.species",
        temporal_scope="PRESENT",
    )
    trace = ProcessingTrace(candidates=[source])
    trace.start(["C1"], "CHARACTER_STAGE2", forwarded=True)
    trace.complete(decision)
    payload = dict(
        scenario_id="S1",
        stage1=[source],
        stage2=[decision],
        processing_version=1,
        processing=trace.records,
    )
    if corruption == "missing":
        payload["processing"] = []
    elif corruption == "duplicate":
        payload["processing"] *= 2
    elif corruption == "unknown":
        payload["processing"] = [trace.records[0].model_copy(update={"candidate_id": "other"})]
    elif corruption == "contradiction":
        payload["stage2"] = []
    elif corruption == "operation":
        payload["processing"] = [trace.records[0].model_copy(update={"operation": "ADD"})]
    else:
        payload["stage1"] = [_character("CHARACTER_DISCOVERY")]
    with pytest.raises(ValueError):
        ScenarioPrediction(**payload)


def test_legacy_only_recovers_stored_facts():
    sources = [
        _character("CHARACTER_DISCOVERY", "UNRESOLVED", "d"),
        _character(match="UNRESOLVED", candidate_id="w"),
        _character(match="AMBIGUOUS", candidate_id="a"),
        _character(match=None, candidate_id="unknown"),
    ]
    scenario = ScenarioPrediction(scenario_id="S1", stage1=sources)
    outcomes = processing_outcomes(scenario)
    assert [v["status"] for v in outcomes.values()] == [
        "NOT_APPLICABLE",
        "WAITING_FOR_CHARACTER",
        "AMBIGUOUS_CHARACTER",
        "RECORD_UNAVAILABLE",
    ]
    assert outcomes["unknown"]["comparisonForwarded"] is None


@pytest.mark.parametrize(
    "forwarded,status", [(False, "PREPARATION_FAILED"), (True, "COMPARISON_FAILED")]
)
def test_failure_safe_code_and_public_redaction(forwarded, status):
    source = _character()
    trace = ProcessingTrace(candidates=[source])
    trace.start(
        ["C1"], "CHARACTER_STAGE2" if forwarded else "CHARACTER_PREPARATION", forwarded=forwarded
    )
    trace.fail(["C1"], "CHARACTER", httpx.ConnectError("SECRET_TOKEN source manuscript"))
    assert trace.records[0].status == status
    assert trace.records[0].failure_code == AnalysisFailureCode.LLM_NETWORK_ERROR
    gold = _empty_gold("CHARACTER")
    bundle = _bundle(gold, [source], [])
    bundle.scenarios[0] = ScenarioPrediction(
        scenario_id="S1", stage1=[source], processing_version=1, processing=trace.records
    )
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    report["scenarios"][0]["processing"][0].update(
        {
            "message": "SECRET_TOKEN",
            "evidenceSpans": [{"quote": "SECRET_MANUSCRIPT"}],
            "rawAiResult": "SECRET_RESULT",
        }
    )
    public = json.dumps(build_public_diagnostics(report), ensure_ascii=False)
    assert "SECRET" not in public
    assert "SECRET" not in render_markdown_summary(report)


def test_abort_keeps_completed_failure_and_unreached_candidates_separate():
    sources = [_world_source(i) for i in ["done", "failed", "unreached"]]
    trace = ProcessingTrace(candidates=sources)
    trace.start(["done"], "WORLD_STAGE2", forwarded=True)
    trace.complete(_world_decision("done"))
    trace.start(["failed"], "WORLD_STAGE2", forwarded=True)
    partial = trace.aborted("S1", httpx.ConnectError("SECRET"))
    assert partial.pipeline_status == "EXECUTION_ABORTED"
    assert [r.status for r in partial.processing] == [
        "COMPARED",
        "COMPARISON_FAILED",
        "EXECUTION_ABORTED",
    ]
    assert [r.comparison_forwarded for r in partial.processing] == [True, True, False]
    assert "SECRET" not in json.dumps([r.model_dump(mode="json") for r in partial.processing])


def test_real_runtime_abort_attaches_partial_bundle():
    gold = _empty_gold("WORLD")
    gold.scenarios[0] = gold.scenarios[0].model_copy(
        update={"source_text": "세계관 후보를 추출하는 원문"}
    )
    with pytest.raises(Exception) as caught:
        asyncio.run(
            run_multi_stage_predictions(
                gold,
                mode="FIXED",
                domains={"WORLD"},
                components=RuntimeComponents(
                    character_comparator=_AddCharacterComparator(),
                    world_extractor=_IndependentWorldPropertiesExtractor(),
                    world_comparator=_IncompleteWorldComparator(),
                ),
            )
        )
    partial = caught.value.prediction_bundle
    assert partial.scenarios[0].processing_version == 1
    assert len(partial.scenarios[0].processing) == len(partial.scenarios[0].stage1) > 0
    PredictionBundleV3.model_validate_json(partial.model_dump_json())


def test_missing_comparator_decisions_are_not_recorded_as_success():
    source = _character()
    trace = ProcessingTrace(candidates=[source])
    trace.start(["C1"], "CHARACTER_STAGE2", forwarded=True)
    with pytest.raises(ValueError, match="cover every handoff"):
        ScenarioPrediction(
            scenario_id="S1", stage1=[source], processing_version=1, processing=trace.records
        )


def test_legacy_report_recovery_never_uses_gold_policy_as_execution_reason():
    gold = _empty_gold("CHARACTER")
    sources = [
        _character("CHARACTER_DISCOVERY", "UNRESOLVED", "discovery"),
        _character(candidate_id="unknown"),
    ]
    report = asyncio.run(evaluate_multi_stage(gold, _bundle(gold, sources, [])))
    scenario = report["scenarios"][0]
    scenario.pop("processing")
    scenario.pop("processingVersion")
    for case in scenario["stage1"]["CHARACTER"]["cases"]:
        case.pop("processing", None)
        case["stage2Policy"] = "WAIT_FOR_CHARACTER_MATCH"
    for case in scenario["stage2"]:
        case.pop("processing", None)
    rows = build_public_diagnostics(report)[0]["processing"]
    assert [row["status"] for row in rows] == ["NOT_APPLICABLE", "RECORD_UNAVAILABLE"]
    assert "재실행" in render_markdown_summary(report)


def test_public_report_rejects_new_missing_processing():
    source = _character()
    gold = _empty_gold("CHARACTER")
    report = asyncio.run(evaluate_multi_stage(gold, _bundle(gold, [source], [])))
    report["scenarios"][0].update(processingVersion=1, processing=[])
    with pytest.raises(ValueError, match="처리 기록 오류"):
        build_public_diagnostics(report)


def test_interrupted_cli_writes_same_safe_records_as_markdown(tmp_path, monkeypatch, capsys):
    from evals.multi_stage_setting import report_cli

    source = _character()
    gold = _empty_gold("CHARACTER")
    bundle = _bundle(gold, [source], [])
    trace = ProcessingTrace(candidates=[source])
    trace.start([source.candidate_id], "CHARACTER_STAGE2", forwarded=True)
    bundle.scenarios[0] = trace.aborted("S1", httpx.ConnectError("SECRET_EXCEPTION"))
    predictions = tmp_path / "predictions.json"
    predictions.write_text(bundle.model_dump_json(by_alias=True))
    markdown = tmp_path / "summary.md"
    monkeypatch.setattr(
        "sys.argv",
        ["report_cli", "--predictions", str(predictions), "--markdown-output", str(markdown)],
    )
    report_cli.main()
    public = json.loads((tmp_path / "diagnostics.json").read_text())
    assert public["scenarios"][0]["processing"][0]["status"] == "COMPARISON_FAILED"
    rendered = markdown.read_text()
    assert "비교 실행 실패" in rendered and "미채점" in rendered
    assert "SECRET" not in rendered and "SECRET" not in json.dumps(public)
    assert "1차 추출 집계" not in rendered
    capsys.readouterr()


def test_gold_matched_candidate_without_decision_keeps_subject_and_reason():
    gold = _world_gold()
    source = _world_source("P1")
    trace = ProcessingTrace(candidates=[source])
    trace.start([source.candidate_id], "WORLD_PREPARATION")
    trace.fail([source.candidate_id], "WORLD", ValueError("SECRET"))
    bundle = _bundle(gold, [source], [])
    bundle.scenarios[0] = ScenarioPrediction(
        scenario_id="S1", stage1=[source], processing_version=1, processing=trace.records
    )
    report = asyncio.run(evaluate_multi_stage(gold, bundle))
    row = build_public_diagnostics(report)[0]["stage2"][0]
    assert row["sourceCandidateId"] == "P1"
    assert row["source"]["subject"]
    assert row["source"]["path"]
    assert row["processing"]["status"] == "PREPARATION_FAILED"
    assert "비교 준비 실패" in render_markdown_summary(report)


def test_oracle_merged_gold_sources_each_receive_the_runtime_decision():
    from tests.test_multi_stage_setting_runtime_adapter import _BatchCapturingWorldComparator

    gold = _world_gold()
    gold.stage1.append(gold.stage1[0].model_copy(update={"gold_id": "W2", "sort_order": 2}))
    gold.stage2[0] = gold.stage2[0].model_copy(update={"source_gold_ids": ["W1", "W2"]})
    gold = gold.with_fixture_hash()
    bundle = asyncio.run(
        run_multi_stage_predictions(
            gold,
            mode="ORACLE",
            domains={"WORLD"},
            components=RuntimeComponents(
                character_comparator=_AddCharacterComparator(),
                world_comparator=_BatchCapturingWorldComparator(),
            ),
        )
    )
    scenario = bundle.scenarios[0]
    assert scenario.stage2[0].source_candidate_ids == ["W1", "W2"]
    assert {row.candidate_id for row in scenario.processing} == {"W1", "W2"}
    assert all(
        row.status == "COMPARED" and row.decision_source_candidate_id == "W1"
        for row in scenario.processing
    )
    ScenarioPrediction.model_validate_json(scenario.model_dump_json())


def test_aggregate_only_legacy_report_explicitly_requires_rerun():
    gold = _empty_gold("CHARACTER")
    report = asyncio.run(evaluate_multi_stage(gold, _bundle(gold, [_character()], [])))
    aggregate = build_source_free_summary(report)
    assert "후보별 처리 기록이 없어" in render_markdown_summary(aggregate)
