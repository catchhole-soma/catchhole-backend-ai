"""Adversarial checks for the evaluator, not accuracy tests of a deployed model."""

from hashlib import sha256
import json
from pathlib import Path
import socket

import pytest
from pydantic import ValidationError

from app.chatbot.evaluation.__main__ import main, markdown
from app.chatbot.evaluation.contracts import (
    MODES,
    Dataset,
    ReviewSet,
    Run,
    fingerprint,
)
from app.chatbot.evaluation.scoring import compare, export_inputs, score

FIXTURE = Path(__file__).parent / "fixtures/chat-evaluation-v1/dataset.json"
DATA = Dataset.model_validate_json(FIXTURE.read_text())


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Offline evaluation must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)
    monkeypatch.setenv("LLM_API_KEY", "must-not-read")
    monkeypatch.setenv("DATABASE_URL", "must-not-connect")


def citation(key):
    evidence = next(e for e in DATA.evidence if e.id == key)
    return dict(
        evidence_id=key,
        version=evidence.version,
        sha256=evidence.sha256,
        quote=evidence.text,
        start=0,
        end=len(evidence.text),
    )


def observation(
    case="ring_reason", text="아리아는 추적을 피하려고 반지를 강물에 던졌다.", evidence="ring-chunk"
):
    return dict(
        case_id=case,
        status="COMPLETED",
        outcome="ANSWER",
        text=text,
        observed_evidence_ids=[evidence],
        citations=[citation(evidence)],
        calls=[],
        telemetry_complete=False,
        latency_ms=1,
        failure_code=None,
    )


def run(observations=(), mode="HYBRID", **manifest):
    value = dict(
        schema_version=1,
        run_id=f"control-{mode}",
        dataset_sha256=fingerprint(DATA),
        mode=mode,
        execution_kind="AUTHORED_CONTROL",
        trace_sha256=sha256(b"synthetic trace").hexdigest(),
        conditions=dict(
            code_sha="b370ed066d5009f6830f45a863fefbbd6fef1228",
            model="authored",
            reasoning="unmeasured",
            prompt_versions={"control": "v1"},
            index_version="synthetic-v1",
            embedding_model="unmeasured",
            embedding_dimensions=1536,
            cache="UNMEASURED",
            concurrency=1,
        ),
        budgets={"max_tool_calls": 16, "max_selected_rows": 10},
        pricing_basis=None,
    )
    value.update(manifest)
    captured = Run.model_validate(dict(manifest=value, observations=list(observations)))
    return captured.model_copy(
        update={
            "manifest": captured.manifest.model_copy(
                update={
                    "trace_sha256": sha256(trace_bytes(captured)).hexdigest(),
                }
            )
        }
    )


def trace_bytes(captured):
    return json.dumps(
        {
            "kind": "AUTHORED_CONTROL",
            "observations_sha256": fingerprint(
                [item.model_dump(mode="json") for item in captured.observations]
            ),
        }
    ).encode()


def review(
    run_value,
    *,
    correct="CORRECT",
    support="SUPPORTED",
    facts=("ring-reason",),
    reviewer_kind="INTERNAL_MODEL",
    forbidden=False,
    complete=True,
):
    observed = run_value.observations[0]
    return ReviewSet.model_validate(
        dict(
            dataset_sha256=fingerprint(DATA),
            run_sha256=fingerprint(run_value),
            reviews=[
                dict(
                    case_id=observed.case_id,
                    observation_sha256=fingerprint(observed),
                    reviewer_kind=reviewer_kind,
                    reviewer="synthetic-test-control",
                    all_assertions_reviewed=complete,
                    disposition_appropriate=True,
                    forbidden_assertion_found=forbidden,
                    rationale="Explicit authored scorer control.",
                    claims=[
                        dict(
                            start=0,
                            end=len(observed.text),
                            quote=observed.text,
                            correctness=correct,
                            support=support,
                            fact_ids=list(facts),
                            evidence_ids=[c.evidence_id for c in observed.citations],
                            rationale="Inspect the cited content, not merely its valid ID.",
                        )
                    ],
                )
            ],
        )
    )


def row(report, name="ring_reason"):
    return next(r for r in report["cases"] if r["case_id"] == name)


def test_dataset_references_hashes_and_all_planned_categories_are_valid():
    assert len(DATA.questions) == 26
    assert len(DATA.evidence) == 72
    assert all(set(q.expected_outcomes) == set(MODES) for q in DATA.questions)
    long_case = next(q for q in DATA.questions if q.id == "long_history_complete")
    assert len(long_case.required_facts) == 45
    assert DATA.gold_status == "INTERNAL_REVIEWED_DRAFT"


@pytest.mark.parametrize(
    "mutation",
    [
        "text_hash",
        "unknown_support",
        "cross_work_support",
        "purged_support",
        "duplicate_case",
        "missing_mode",
    ],
)
def test_invalid_gold_is_rejected_before_scoring(mutation):
    value = DATA.model_dump(mode="json")
    if mutation == "text_hash":
        value["evidence"][0]["text"] += "tamper"
    elif mutation == "unknown_support":
        value["questions"][0]["required_facts"][0]["support_sets"] = [["invented"]]
    elif mutation == "cross_work_support":
        value["questions"][0]["required_facts"][0]["support_sets"] = [["foreign-chunk"]]
    elif mutation == "purged_support":
        value["questions"][0]["required_facts"][0]["support_sets"] = [["deleted-chunk"]]
    elif mutation == "duplicate_case":
        value["questions"].append(value["questions"][0])
    else:
        del value["questions"][0]["expected_outcomes"]["VECTOR_ONLY"]
    with pytest.raises(ValidationError):
        Dataset.model_validate(value)


def test_exported_execution_inputs_cannot_leak_gold_or_scripted_answers():
    exported = export_inputs(DATA)
    assert len(exported["questions"]) == len(DATA.questions)
    assert "conversation" in exported["questions"][0]
    encoded = json.dumps(exported)
    for forbidden in (
        "required_facts",
        "expected_outcomes",
        "support_sets",
        "review_guidance",
        "forbidden_assertions",
        "modelOutputs",
    ):
        assert forbidden not in encoded


def test_valid_citations_without_semantic_review_remain_pending():
    observed = run([observation()])
    report = score(DATA, observed)
    assert row(report)["verdict"] == "PENDING"
    assert row(report)["violations"] == []
    assert report["summary"]["missing"] == 25
    assert report["summary"]["reviewed_claim_correctness"]["value"] is None
    assert report["release_ready"] is False


def test_valid_weather_citation_for_unrelated_ring_claim_is_not_a_semantic_pass():
    observed = run([observation(evidence="weather-chunk")])
    report = score(DATA, observed, review(observed, support="UNSUPPORTED"))
    assert row(report)["verdict"] == "FAIL"
    assert row(report)["violations"] == ["REQUIRED_FACTS_MISSING"]
    assert report["summary"]["reviewed_evidence_support"]["value"] == 0
    assert row(report)["covered_facts"] == 0


def test_reviewed_correct_answer_counts_only_the_reviewed_observation():
    observed = run([observation()])
    report = score(DATA, observed, review(observed))
    assert row(report)["verdict"] == "PASS"
    assert report["summary"]["review_coverage"] == {
        "numerator": 1,
        "denominator": 26,
        "value": 1 / 26,
    }
    assert report["measurements"]["total_cost_usd"] is None
    assert report["measurements"]["p95_ms"] is None


@pytest.mark.parametrize(
    "change", ["answer", "dataset", "span", "fact", "unobserved", "duplicate_span"]
)
def test_stale_or_fabricated_reviews_are_rejected(change):
    observed = run([observation()])
    value = review(observed).model_dump(mode="json")
    if change == "answer":
        value["reviews"][0]["observation_sha256"] = "0" * 64
    elif change == "dataset":
        value["dataset_sha256"] = "0" * 64
    elif change == "span":
        value["reviews"][0]["claims"][0]["quote"] = "not in actual answer"
    elif change == "fact":
        value["reviews"][0]["claims"][0]["fact_ids"] = ["invented"]
    elif change == "unobserved":
        value["reviews"][0]["claims"][0]["evidence_ids"] = ["weather-chunk"]
    else:
        value["reviews"][0]["claims"] *= 2
    with pytest.raises(ValueError):
        score(DATA, observed, ReviewSet.model_validate(value))


@pytest.mark.parametrize("evidence", ["foreign-chunk", "deleted-chunk", "old-chunk"])
def test_other_work_purged_and_replaced_evidence_fail_boundaries(evidence):
    observed = run([observation(evidence=evidence)])
    assert row(score(DATA, observed))["verdict"] == "FAIL"


@pytest.mark.parametrize(
    "field,value",
    [("version", "wrong-v2"), ("sha256", "0" * 64), ("quote", "tampered"), ("end", 9999)],
)
def test_version_hash_quote_and_offset_mismatch_are_rejected(field, value):
    item = observation()
    item["citations"][0][field] = value
    assert row(score(DATA, run([item])))["verdict"] == "FAIL"


def test_citing_corpus_evidence_never_seen_by_the_run_is_a_failure():
    item = observation()
    item["observed_evidence_ids"] = []
    assert "CITATION_NOT_OBSERVED" in row(score(DATA, run([item])))["violations"]


def test_source_episode_filter_does_not_turn_current_rows_into_historical_snapshots():
    item = observation("episode_bound", evidence="level-chunk-7")
    assert row(score(DATA, run([item])), "episode_bound")["verdict"] == "FAIL"
    item = observation("direct_source_cost", evidence="fire-current")
    # Current rows are visible, but do not support the expected historical 35 fact.
    observed = run([item])
    result = score(DATA, observed, review(observed, facts=("source-35",)))
    assert row(result, "direct_source_cost")["covered_facts"] == 0
    assert row(result, "direct_source_cost")["verdict"] == "FAIL"


def test_vector_only_cannot_claim_access_to_manual_rdb_or_unembedded_chunks():
    for case, evidence in [
        ("current_manual_override", "fire-current"),
        ("index_unready", "unindexed-chunk"),
    ]:
        result = score(DATA, run([observation(case, evidence=evidence)], "VECTOR_ONLY"))
        assert row(result, case)["information"] == "LIMITED"
        assert row(result, case)["verdict"] == "FAIL"


def test_45_history_records_cannot_pass_with_just_one_last_record():
    observed = run(
        [observation("long_history_complete", "45화에서 46으로 바뀌었습니다.", "long-history-45")]
    )
    result = score(DATA, observed, review(observed, facts=("sein-45",)))
    actual = row(result, "long_history_complete")
    assert actual["required_facts"] == 45 and actual["covered_facts"] == 1
    assert actual["verdict"] == "FAIL"


def test_blanket_abstention_cannot_hide_behind_high_claim_accuracy():
    items = [
        dict(
            case_id=q.id,
            status="COMPLETED",
            outcome="INSUFFICIENT_EVIDENCE",
            text="확인할 근거가 부족합니다.",
        )
        for q in DATA.questions
    ]
    report = score(DATA, run(items))
    assert report["summary"]["answerable_response_rate"]["value"] == 0
    assert report["summary"]["reviewed_claim_correctness"]["value"] is None
    assert report["summary"]["required_fact_coverage"]["numerator"] == 0


@pytest.mark.parametrize("kind", ["HUMAN", "INTERNAL_MODEL", "LLM_JUDGE"])
def test_reviewer_origin_is_visible_and_judge_alone_cannot_sign_off(kind):
    observed = run([observation()])
    result = score(DATA, observed, review(observed, reviewer_kind=kind))
    assert row(result)["reviewer_kind"] == kind
    assert row(result)["verdict"] == ("PENDING" if kind == "LLM_JUDGE" else "PASS")
    assert result["release_ready"] is False


def test_unknown_semantics_and_partial_review_remain_pending():
    observed = run([observation()])
    for reviewed in (review(observed, correct="UNCERTAIN"), review(observed, complete=False)):
        assert row(score(DATA, observed, reviewed))["verdict"] == "PENDING"


def test_failed_incomplete_and_missing_cases_keep_the_full_denominator():
    observed = run(
        [
            dict(case_id="ring_reason", status="FAILED", failure_code="TIMEOUT"),
            dict(case_id="ring_scene", status="INCOMPLETE"),
        ]
    )
    summary = score(DATA, observed)["summary"]
    assert summary["execution_completion"]["denominator"] == 26
    assert summary["verdicts"] == {"PENDING": 24, "FAIL": 2}
    assert summary["missing"] == 24


def test_duplicate_case_call_and_unknown_case_do_not_inflate_scores():
    item = observation()
    with pytest.raises(ValidationError):
        run([item, item])
    call = dict(call_id="same", purpose="PLAN", outcome="UNKNOWN")
    item["calls"] = [call, call]
    with pytest.raises(ValidationError):
        run([item])
    with pytest.raises(ValueError):
        score(DATA, run([observation("invented")]))


def test_missing_usage_is_not_zero_and_failed_calls_are_included():
    item = observation()
    item["calls"] = [
        dict(
            call_id="failure",
            purpose="PLAN",
            outcome="FAILURE",
            input_tokens=10,
            cached_input_tokens=2,
            output_tokens=5,
            cost_usd=0.01,
        ),
        dict(call_id="unknown", purpose="ANSWER", outcome="UNKNOWN"),
    ]
    item["telemetry_complete"] = True
    result = score(DATA, run([item], execution_kind="PROVIDER_CAPTURE", pricing_basis="test-only"))
    metrics = result["measurements"]
    assert metrics["failed_calls"] == 1 and metrics["unknown_usage_calls"] == 1
    assert metrics["total_cost_usd"] is None and metrics["input_tokens"] is None
    assert metrics["known_cost_subtotal_usd"] == 0.01


@pytest.mark.parametrize(
    "updates",
    [
        dict(input_tokens=1),
        dict(cost_usd=0),
        dict(input_tokens=1, cached_input_tokens=2, output_tokens=0),
    ],
)
def test_malformed_usage_cannot_be_scored(updates):
    item = observation()
    item["calls"] = [dict(call_id="bad", purpose="PLAN", outcome="UNKNOWN", **updates)]
    with pytest.raises(ValidationError):
        run([item])


def test_comparison_requires_three_modes_and_identical_conditions():
    runs = [run(mode=mode) for mode in MODES]
    result = compare(DATA, runs)
    assert set(result["reports"]) == set(MODES)
    assert all(r["summary"]["missing"] == 26 for r in result["reports"].values())
    with pytest.raises(ValueError):
        compare(DATA, runs[:2])
    changed = runs[1].model_dump(mode="json")
    changed["manifest"]["conditions"]["model"] = "different-model"
    with pytest.raises(ValueError):
        compare(DATA, [runs[0], Run.model_validate(changed), runs[2]])


def test_cli_trace_binding_json_markdown_consistency_and_permissions(tmp_path):
    observed = run([observation()])
    run_file, review_file, trace_file = [
        tmp_path / n for n in ("run.json", "review.json", "trace.json")
    ]
    run_file.write_text(observed.model_dump_json())
    review_file.write_text(review(observed).model_dump_json())
    trace_file.write_bytes(trace_bytes(observed))
    out = tmp_path / "result"
    args = [
        "score",
        "--dataset",
        str(FIXTURE),
        "--run",
        str(run_file),
        "--trace",
        str(trace_file),
        "--reviews",
        str(review_file),
        "--output-dir",
        str(out),
    ]
    assert main(args) == 0
    report = json.loads((out / "report.json").read_text())
    assert (out / "summary.md").read_text() == markdown(report)
    assert (out / "report.json").stat().st_mode & 0o777 == 0o600
    assert report["summary"]["cases"] == 26
    trace_file.write_bytes(b"modified")
    args[-1] = str(tmp_path / "reject")
    with pytest.raises(SystemExit) as error:
        main(args)
    assert error.value.code == 2 and not (tmp_path / "reject").exists()


def test_compare_cli_keeps_unknown_reviews_and_preserves_different_budgets(tmp_path):
    runs, traces = [], []
    for index, mode in enumerate(MODES):
        observed = run(mode=mode, budgets={"max_tool_calls": index + 1})
        run_path, trace_path = tmp_path / f"{mode}.json", tmp_path / f"{mode}.trace"
        run_path.write_text(observed.model_dump_json())
        trace_path.write_bytes(trace_bytes(observed))
        runs.append(str(run_path))
        traces.append(str(trace_path))
    target = tmp_path / "compared"
    assert (
        main(
            [
                "compare",
                "--dataset",
                str(FIXTURE),
                "--run",
                *runs,
                "--trace",
                *traces,
                "--output-dir",
                str(target),
            ]
        )
        == 0
    )
    report = json.loads((target / "report.json").read_text())
    assert len(report["reports"]) == 3
    for index, mode in enumerate(MODES):
        item = report["reports"][mode]
        assert item["summary"]["verdicts"] == {"PENDING": 26}
        assert item["manifest"]["budgets"]["max_tool_calls"] == index + 1
        assert item["measurements"]["total_cost_usd"] is None


def test_incomplete_run_cannot_publish_a_complete_cost_total_even_with_known_call_usage():
    items = [
        dict(case_id=q.id, status="FAILED", failure_code="TIMEOUT", telemetry_complete=True)
        for q in DATA.questions
    ]
    items[0]["status"] = "INCOMPLETE"
    report = score(DATA, run(items, execution_kind="PROVIDER_CAPTURE", pricing_basis="test-only"))
    assert report["measurements"]["telemetry_complete"] is False
    assert report["measurements"]["total_cost_usd"] is None


def test_cli_does_not_print_private_validation_values(tmp_path, capsys):
    private = tmp_path / "bad.json"
    private.write_text('{"unexpected": "PRIVATE_RAW_ANSWER"}')
    with pytest.raises(SystemExit):
        main(["validate", "--dataset", str(private)])
    assert "PRIVATE_RAW_ANSWER" not in capsys.readouterr().err


def test_normalized_answer_cannot_change_while_reusing_the_old_trace(tmp_path):
    observed = run([observation()])
    trace_file, run_file = tmp_path / "trace.json", tmp_path / "run.json"
    trace_file.write_bytes(trace_bytes(observed))
    value = observed.model_dump(mode="json")
    value["observations"][0]["text"] += "추가로 지어낸 답변"
    run_file.write_text(json.dumps(value))
    with pytest.raises(SystemExit) as error:
        main(
            [
                "score",
                "--dataset",
                str(FIXTURE),
                "--run",
                str(run_file),
                "--trace",
                str(trace_file),
                "--output-dir",
                str(tmp_path / "score"),
            ]
        )
    assert error.value.code == 2


def test_cli_export_never_overwrites_an_existing_file(tmp_path):
    target = tmp_path / "existing.json"
    target.write_text("keep")
    with pytest.raises(SystemExit):
        main(["export-inputs", "--dataset", str(FIXTURE), "--output", str(target)])
    assert target.read_text() == "keep"


def test_scripted_runtime_capture_scores_actual_rendered_answers_and_keeps_missing_cases(tmp_path):
    from tests.support.chat_evaluation_replay import main as replay_main

    capture = tmp_path / "capture"
    assert replay_main(["--output-dir", str(capture)]) == 0
    observed = Run.model_validate_json((capture / "run.json").read_text())
    trace = json.loads((capture / "trace.json").read_text())
    assert len(observed.observations) == 2
    assert observed.manifest.execution_kind == "SCRIPTED_REPLAY"
    for item, original in zip(observed.observations, trace["executions"], strict=True):
        assert item.text == original["finish"]["result"]["text"]
        assert len(item.calls) == 2 and item.telemetry_complete
        assert item.outcome == "ANSWER" and "42" in item.text
        assert [q["tool"] for q in original["queries"]] == ["FIND_WORLD_SETTINGS", "WORLD_CURRENT"]
        assert original["modelRequests"][0]["input"].get("conversation", []) == (
            [{"question": "화염술의 현재 등록 소모 마력은?", "answer": "35입니다."}]
            if item.case_id == "followup_refresh"
            else []
        )
    report = score(DATA, observed)
    assert report["summary"]["missing"] == 24
    assert report["summary"]["verdicts"] == {"PENDING": 26}
    assert report["measurements"]["provider_calls_recorded"] == 4
    assert report["measurements"]["total_cost_usd"] is None
