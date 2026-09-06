import json
import sys

import pytest

from evals.multi_stage_setting.contracts import (
    CharacterStage1Gold,
    CharacterStage2Gold,
    CharacterStateEntry,
    EvaluationState,
    GoldSnapshotV3,
    KnownCharacter,
    ReviewStatus,
    ScenarioGold,
    StartStateMode,
    WorldStage1Gold,
    WorldStage2Gold,
    WorldStateEntry,
    character_state_ref,
    world_state_ref,
)
from evals.multi_stage_setting.loaders import load_gold_snapshot_v3
from evals.multi_stage_setting.report_cli import (
    _append_diagnostics,
    build_public_diagnostics,
    build_source_free_summary,
    render_markdown_summary,
)
from evals.multi_stage_setting.state_cli import main as state_cli_main
from evals.multi_stage_setting.state_effects import build_gold_state_chain


def test_v3_loader_verifies_fixture_hash_before_attaching_sources(tmp_path) -> None:
    gold = _gold().with_fixture_hash()
    path = tmp_path / "gold.json"
    path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = load_gold_snapshot_v3(path)

    assert loaded.fixture_hash == gold.fixture_hash

    tampered = json.loads(path.read_text(encoding="utf-8"))
    tampered["name"] = "tampered"
    path.write_text(json.dumps(tampered, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError, match="fixtureHash does not match"):
        load_gold_snapshot_v3(path)


def test_v3_loader_rejects_versionless_payload_and_source_root_escape(tmp_path) -> None:
    versionless = tmp_path / "versionless.json"
    versionless.write_text('{"name":"legacy"}', encoding="utf-8")
    with pytest.raises(ValueError, match="legacy setting_extraction CLI"):
        load_gold_snapshot_v3(versionless)

    scenario = _gold().scenarios[0].model_copy(update={"source_identifier": "../secret.txt"})
    escaping = GoldSnapshotV3(
        dataset_version="v3",
        name="escape",
        scenarios=[scenario],
    ).with_fixture_hash()
    escaping_path = tmp_path / "escaping.json"
    escaping_path.write_text(
        json.dumps(escaping.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    source_root = tmp_path / "sources"
    source_root.mkdir()

    with pytest.raises(ValueError, match="escapes the source root"):
        load_gold_snapshot_v3(escaping_path, source_root=source_root)


def test_v3_loader_maps_author_local_absolute_source_to_episode_pattern(tmp_path) -> None:
    source_root = tmp_path / "sources"
    source_root.mkdir()
    source_text = "실제 평가 원문"
    (source_root / "01화.txt").write_text(source_text, encoding="utf-8")
    scenario = _gold().scenarios[0].model_copy(
        update={"source_identifier": "/Users/author/private/01화.txt"}
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="absolute-author-source",
        scenarios=[scenario],
    ).with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )

    loaded = load_gold_snapshot_v3(gold_path, source_root=source_root)

    assert loaded.scenarios[0].source_text == source_text


def test_external_seed_state_requires_a_content_hash(tmp_path) -> None:
    scenario = _gold().scenarios[0].model_copy(
        update={
            "start_state_mode": StartStateMode.SEED,
            "before_state_uri": "seed.json",
        }
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="external-seed",
        scenarios=[scenario],
    ).with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    state_root = tmp_path / "states"
    state_root.mkdir()
    (state_root / "seed.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="has no beforeState hash"):
        load_gold_snapshot_v3(gold_path, state_root=state_root)


def test_public_report_allowlists_diagnostics_and_keeps_score_json_aggregate_only() -> None:
    report = {
        "reportVersion": "setting-eval-report/v3",
        "run": {
            "mode": "FIXED",
            "domains": ["CHARACTER"],
            "analysisModel": "extractor",
            "subjectResolutionModel": "resolver",
            "comparisonModel": "comparator",
            "inputTokens": 10,
            "cachedInputTokens": 2,
            "outputTokens": 3,
            "runtimeFailures": {
                "total": 2,
                "byStage": {"CHARACTER_STAGE1": 2},
                "byErrorType": {"INVALID_RESPONSE": 2},
                "messages": ["SECRET_RUNTIME_FAILURE"],
            },
        },
        "dataset": {
            "name": "test",
            "version": "v3",
            "episodes": [1],
            "fixtureHash": "sha256:test",
        },
        "stages": {
            "character": {
                "stage1": {
                    "metrics": {
                        "candidatePrecision": 0.5,
                        "candidateRecall": 0.5,
                        "candidateF1": 0.5,
                        "weightedRecall": 0.75,
                        "entityOrSubjectAccuracy": 1,
                        "pathOrFactAccuracy": 0.5,
                        "valueAccuracy": 0.5,
                        "evidenceLocatableRate": 1,
                        "evidenceCoverageRate": 0.5,
                    },
                    "counts": {
                        "gold": 3,
                        "predictions": 3,
                        "matches": 2,
                        "identityTruePositive": 1,
                        "missed": 1,
                        "extra": 1,
                    },
                },
                "stage2": {
                    "metrics": {
                        "upstreamReachRate": 1,
                        "fullDecisionAccuracy": 0,
                        "operationAccuracy": 0,
                        "targetAccuracy": 1,
                        "proposedValueAccuracy": 1,
                        "characterCanonicalFactKeyResolutionAccuracy": 1,
                        "temporalAccuracy": 1,
                    },
                    "counts": {
                        "gold": 1,
                        "upstreamReached": 1,
                        "reachedAndCompared": 1,
                    },
                    "matches": [{"evidence": "SECRET_STAGE_MATCH"}],
                },
            },
            "world": {
                "stage1": {"evaluated": False, "reason": "Domain not selected."},
                "stage2": {"evaluated": False, "reason": "Domain not selected."},
            },
            "macroAverage": {"stage1CandidateF1": 1},
        },
        "endToEnd": {
            "metrics": {
                "afterStateF1": 0.5,
                "transitionPrecision": 0.5,
                "transitionRecall": 0.5,
                "transitionF1": 0.5,
            },
            "counts": {
                "stateApplicationErrors": 0,
                "dependencyStateApplicationErrors": 0,
                "matchedTransitions": 1,
                "expectedTransitions": 2,
                "predictedTransitions": 2,
            },
            "domains": {
                "CHARACTER": {
                    "afterStatePrecision": 0.5,
                    "afterStateRecall": 0.5,
                    "afterStateF1": 0.5,
                    "semanticCoverage": 1,
                    "semanticPending": 0,
                }
            },
            "scenarios": [{"actualValue": "SECRET_STATE"}],
        },
        "failureCauses": {"COMPARISON_ERROR": 1},
        "scenarios": [
            {
                "scenarioId": "S1",
                "episodeNo": 1,
                "sourceText": "SECRET_SOURCE",
                "stage1": {
                    "CHARACTER": {
                        "cases": [
                            {
                                "result": "PARTIAL_MATCH",
                                "goldIds": ["C1"],
                                "predictionId": "P1",
                                "importance": "MUST",
                                "candidateKind": "SETTING",
                                "expected": {
                                    "subject": "비요른",
                                    "path": "STATUS › status.부상",
                                    "value": "오른발 부상",
                                    "evidenceQuotes": ["SECRET_EXPECTED_EVIDENCE"],
                                },
                                "actual": {
                                    "subject": "비요른",
                                    "path": "STATUS › status.회복",
                                    "value": (
                                        "허용된 값 | <script>x</script>\n</details> "
                                        "![leak](https://example.invalid/x)"
                                    ),
                                    "rawAiResult": "SECRET_RAW",
                                },
                                "fields": {
                                    "subject": "MATCH",
                                    "path": "MISMATCH",
                                    "value": "PENDING",
                                    "evidence": "SECRET_FIELD",
                                },
                                "upstreamOutcome": "REACHED",
                                "evidence": "SECRET_CASE_EVIDENCE",
                            },
                            {
                                "result": "FULL_MATCH",
                                "goldIds": ["C2"],
                                "predictionId": "P2",
                                "expected": {
                                    "subject": "비요른",
                                    "path": "PROFILE › profile.species",
                                    "value": "바바리안",
                                },
                                "actual": {
                                    "subject": "비요른",
                                    "path": "PROFILE › profile.species",
                                    "value": "바바리안",
                                },
                                "fields": {
                                    "subject": "MATCH",
                                    "path": "MATCH",
                                    "value": "MATCH",
                                },
                                "upstreamOutcome": "REACHED",
                            },
                            {
                                "result": "MISSED",
                                "goldIds": ["C3"],
                                "predictionId": None,
                                "expected": {
                                    "subject": "아이나르",
                                    "path": "STATUS › status.부상",
                                    "value": "오른팔 부상",
                                },
                                "actual": None,
                                "fields": {
                                    "subject": "MISSING",
                                    "path": "MISSING",
                                    "value": "MISSING",
                                },
                                "upstreamOutcome": "UPSTREAM_MISSING",
                            },
                            {
                                "result": "EXTRA",
                                "goldIds": [],
                                "predictionId": "P3",
                                "expected": None,
                                "actual": {
                                    "subject": "미상",
                                    "path": "STATUS › status.피곤",
                                    "value": "피곤함",
                                },
                                "fields": {
                                    "subject": "UNMATCHED",
                                    "path": "UNMATCHED",
                                    "value": "UNMATCHED",
                                },
                                "upstreamOutcome": "UPSTREAM_EXTRA",
                            },
                        ]
                    }
                },
                "stage2": [
                    {
                        "result": "DECISION_MISMATCH",
                        "decisionId": "D1",
                        "domain": "CHARACTER",
                        "sourceGoldIds": ["C1"],
                        "sourceCandidateId": "P1",
                        "upstreamOutcome": "REACHED",
                        "failureCause": "COMPARISON_ERROR",
                        "expected": {
                            "operation": "REMOVE",
                            "path": "status.회복",
                            "value": None,
                            "temporalScope": "PRESENT",
                            "removedCount": 2,
                            "removedPaths": [
                                "비요른 · STATUS › status.오른발_부상",
                                "비요른 · STATUS › status.마비독",
                            ],
                            "targetRef": "SECRET_TARGET",
                        },
                        "actual": {
                            "operation": "ADD",
                            "path": "status.회복",
                            "value": "빠르게 회복 중",
                            "temporalScope": "PRESENT",
                            "removedCount": 0,
                            "comparisonReason": "SECRET_REASON",
                        },
                        "fields": {
                            "operation": "MISMATCH",
                            "canonicalPath": "MATCH",
                            "target": "MATCH",
                            "value": "MATCH",
                            "raw": "SECRET_STAGE2_FIELD",
                        },
                        "comparisonReason": "SECRET_COMPARISON_REASON",
                    }
                ],
                "stateErrors": [{"reason": "SECRET_STATE_ERROR"}],
            }
        ],
    }

    summary = build_source_free_summary(report)
    serialized = json.dumps(summary, ensure_ascii=False)
    diagnostics = build_public_diagnostics(report)
    markdown = render_markdown_summary(report)

    assert "scenarios" not in summary
    assert "SECRET_" not in serialized
    assert diagnostics[0]["stage1"]["character"]["cases"][0]["actual"]["value"].startswith(
        "허용된 값"
    )
    assert list(diagnostics[0]["stage1"]["character"]["cases"][0]["fields"]) == [
        "subject",
        "path",
        "value",
    ]
    assert diagnostics[0]["stage2"][0]["expected"]["removedPaths"] == [
        "비요른 · STATUS › status.오른발_부상",
        "비요른 · STATUS › status.마비독",
    ]
    assert "허용된 값" in markdown
    assert "가중 Recall" in markdown
    assert "Gold 3 · 예측 3 · 연결 2 · TP 1 · 누락 1 · 과추출 1" in markdown
    assert "결정 불일치" in markdown
    assert "<summary>완전 일치 1건 보기</summary>" in markdown
    assert "SECRET_" not in markdown
    assert "<script>" not in markdown
    assert "&lt;script&gt;x&lt;/script&gt;" in markdown
    assert "&lt;/details&gt;" in markdown
    assert "허용된 값 &#124;" in markdown
    assert "![leak]" not in markdown
    assert "&#33;&#91;leak&#93;" in markdown
    assert "총 `2`건" in markdown
    assert "CHARACTER_STAGE1" in markdown
    assert "SECRET_RUNTIME_FAILURE" not in markdown


def test_diagnostic_markdown_has_a_global_row_budget() -> None:
    case = {
        "result": "EXTRA",
        "goldIds": [],
        "predictionId": "P",
        "expected": None,
        "actual": {
            "subject": "미상",
            "path": "STATUS › status.피곤",
            "value": "피곤함",
        },
        "fields": {
            "subject": "UNMATCHED",
            "path": "UNMATCHED",
            "value": "UNMATCHED",
        },
        "upstreamOutcome": "UPSTREAM_EXTRA",
    }
    diagnostics = [
        {
            "scenarioId": f"S{scenario_no}",
            "episodeNo": scenario_no,
            "stage1": {
                "character": {
                    "cases": [case | {"predictionId": f"P{scenario_no}-{index}"} for index in range(25)]
                }
            },
            "stage2": [],
        }
        for scenario_no in range(1, 11)
    ]
    lines: list[str] = []

    _append_diagnostics(lines, diagnostics)
    markdown = "\n".join(lines)

    assert "전체 250건 중 200건만 표시" in markdown
    assert len(markdown.encode("utf-8")) < 1_000_000


def test_diagnostic_markdown_reduces_rows_to_stay_under_the_byte_budget() -> None:
    payload = "`" * 180
    case = {
        "result": "PARTIAL_MATCH",
        "goldIds": [payload],
        "predictionId": payload,
        "expected": {"subject": payload, "path": payload, "value": payload},
        "actual": {"subject": payload, "path": payload, "value": payload},
        "fields": {
            "subject": "MISMATCH",
            "path": "MISMATCH",
            "value": "MISMATCH",
        },
        "upstreamOutcome": "REACHED",
    }
    diagnostics = [
        {
            "scenarioId": f"S{scenario_no}",
            "episodeNo": scenario_no,
            "stage1": {
                "character": {
                    "cases": [case | {"predictionId": f"P{scenario_no}-{index}"} for index in range(25)]
                }
            },
            "stage2": [],
        }
        for scenario_no in range(1, 11)
    ]
    lines: list[str] = []

    _append_diagnostics(lines, diagnostics)
    markdown = "\n".join(lines)

    assert "전체 250건 중 100건만 표시" in markdown
    assert len(markdown.encode("utf-8")) <= 900_000


def test_state_cli_generates_verified_hashes_and_updated_gold(tmp_path, monkeypatch) -> None:
    gold = _gold().with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    state_dir = tmp_path / "states"
    updated_path = tmp_path / "gold-with-states.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "state_cli",
            "--gold",
            str(gold_path),
            "--output-dir",
            str(state_dir),
            "--updated-gold",
            str(updated_path),
            "--mode",
            "verified",
        ],
    )

    state_cli_main()

    updated = load_gold_snapshot_v3(updated_path)
    transition = build_gold_state_chain(updated)["S1"]
    assert updated.scenarios[0].state_generation_status == "VERIFIED"
    assert updated.scenarios[0].after_state_hash == (
        f"sha256:{transition.after_state.content_hash()}"
    )
    assert (state_dir / "manifest.json").is_file()
    preview_path = state_dir / "0001-S1.before.notion.md"
    assert preview_path.is_file()
    assert "평가 시작 전 누적 상태 · 자동 생성" in preview_path.read_text(
        encoding="utf-8"
    )
    assert "첫 회차는 빈 상태에서 평가를 시작합니다." in preview_path.read_text(
        encoding="utf-8"
    )


def test_state_cli_materializes_missing_stage2_before_values_from_fixture(
    tmp_path,
    monkeypatch,
) -> None:
    gold = _gold_with_character_update().with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    state_dir = tmp_path / "states"
    updated_path = tmp_path / "gold-with-states.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "state_cli",
            "--gold",
            str(gold_path),
            "--output-dir",
            str(state_dir),
            "--updated-gold",
            str(updated_path),
            "--mode",
            "verified",
        ],
    )

    state_cli_main()

    updated = load_gold_snapshot_v3(updated_path)
    decision = updated.stage2[0]
    assert decision.before_value == "170cm"
    assert decision.before_value_json == {"value": "170cm"}
    world_decision = next(item for item in updated.stage2 if item.decision_id == "D2")
    assert world_decision.before_value == "평균 140cm다."
    assert world_decision.before_value_json is None
    assert updated.scenarios[0].known_character_names == ["비요른", "비요른"]
    assert updated.scenarios[0].provided_context == "knownCharacters=[비요른, 비요른]"
    assert build_gold_state_chain(updated)["S1"].after_state.character_facts[0].value == (
        "180cm"
    )


def test_state_cli_preview_marks_generated_without_official_state_fixtures(
    tmp_path,
    monkeypatch,
) -> None:
    scenario = _gold().scenarios[0].model_copy(
        update={"review_status": ReviewStatus.DRAFT}
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="preview",
        scenarios=[scenario],
    ).with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    state_dir = tmp_path / "states"
    updated_path = tmp_path / "gold-with-preview.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "state_cli",
            "--gold",
            str(gold_path),
            "--output-dir",
            str(state_dir),
            "--updated-gold",
            str(updated_path),
        ],
    )

    state_cli_main()

    updated = load_gold_snapshot_v3(updated_path)
    updated_scenario = updated.scenarios[0]
    assert updated_scenario.state_generation_status == "GENERATED"
    assert updated_scenario.before_state_uri is None
    assert updated_scenario.before_state_hash is None
    assert updated_scenario.after_state_uri is None
    assert updated_scenario.after_state_hash is None
    assert not (state_dir / "0001-S1.before.json").exists()
    assert not (state_dir / "0001-S1.after.json").exists()
    preview_path = state_dir / "0001-S1.before.notion.md"
    assert "검수 전 미리보기" in preview_path.read_text(encoding="utf-8")
    manifest = json.loads((state_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["mode"] == "preview"
    assert manifest["states"][0]["stateGenerationStatus"] == "GENERATED"
    assert "beforeStateUri" not in manifest["states"][0]


def test_state_cli_verified_rejects_non_final_rows(tmp_path, monkeypatch) -> None:
    scenario = _gold().scenarios[0].model_copy(
        update={"review_status": ReviewStatus.DRAFT}
    )
    gold = GoldSnapshotV3(
        dataset_version="v3",
        name="invalid-verified",
        scenarios=[scenario],
    ).with_fixture_hash()
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(
        json.dumps(gold.model_dump(mode="json", by_alias=True), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "state_cli",
            "--gold",
            str(gold_path),
            "--output-dir",
            str(tmp_path / "states"),
            "--mode",
            "verified",
        ],
    )

    with pytest.raises(ValueError, match="requires every included Scenario and Gold row"):
        state_cli_main()


def _gold() -> GoldSnapshotV3:
    return GoldSnapshotV3(
        dataset_version="v3",
        name="io",
        scenarios=[
            ScenarioGold(
                scenario_id="S1",
                episode_no=1,
                source_identifier="01화.txt",
                target_domains={"CHARACTER"},
                gold_version="v3",
                candidate_free=True,
                start_state_mode="EMPTY",
                cumulative_through_episode=0,
                review_status="FINAL",
            )
        ],
    )


def _gold_with_character_update() -> GoldSnapshotV3:
    target_ref = character_state_ref(
        "character:bjorn", "PROFILE", "profile.height"
    )
    seed = EvaluationState(
        known_characters=[
            KnownCharacter(
                entity_ref="character:bjorn-variant",
                name="비요른",
                creation_order=2,
            ),
            KnownCharacter(
                entity_ref="character:bjorn",
                name="비요른",
                creation_order=1,
            ),
            KnownCharacter(
                entity_ref="character:retired",
                name="은퇴자",
                creation_order=3,
                active=False,
            ),
        ],
        character_facts=[
            CharacterStateEntry(
                ref=target_ref,
                entity_ref="character:bjorn",
                entity_name="비요른",
                fact_type="PROFILE",
                fact_key="profile.height",
                value_type="STRING",
                value="170cm",
                value_json={"value": "170cm"},
            )
        ],
        world_facts=[
            WorldStateEntry(
                ref=world_state_ref("RACE", "고블린", None, "체격"),
                category="RACE",
                subject_name="고블린",
                setting_name="체격",
                value="평균 140cm다.",
            )
        ],
    )
    scenario = ScenarioGold(
        scenario_id="S1",
        episode_no=1,
        source_identifier="01화.txt",
        target_domains={"CHARACTER", "WORLD"},
        gold_version="v3",
        start_state_mode="SEED",
        cumulative_through_episode=0,
        seed_state=seed,
        review_status="FINAL",
    )
    source = CharacterStage1Gold(
        gold_id="C1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["키가 180cm가 되었다."],
        review_status="FINAL",
        domain="CHARACTER",
        candidate_kind="SETTING",
        entity_ref="character:bjorn",
        entity_name="비요른",
        fact_type="PROFILE",
        fact_key="profile.height",
        value_type="STRING",
        display_value="180cm",
        value_json={"value": "180cm"},
        value_json_provenance="ANNOTATED",
        structured_scorable=True,
    )
    decision = CharacterStage2Gold(
        decision_id="D1",
        scenario_id="S1",
        episode_no=1,
        sort_order=1,
        source_gold_ids=["C1"],
        domain="CHARACTER",
        operation="UPDATE",
        temporal_scope="PRESENT",
        target_ref=target_ref,
        proposed_value="180cm",
        proposed_value_json={"value": "180cm"},
        review_status="FINAL",
    )
    world_source = WorldStage1Gold(
        gold_id="W1",
        scenario_id="S1",
        episode_no=1,
        sort_order=2,
        decision="EXTRACT",
        importance="MUST",
        evidence_quotes=["고블린의 평균 체격이 커졌다."],
        review_status="FINAL",
        domain="WORLD",
        candidate_kind="WORLD_SETTING",
        category="RACE",
        subject_name="고블린",
        setting_name="체격",
        source_values=["평균 150cm다."],
    )
    world_decision = WorldStage2Gold(
        decision_id="D2",
        scenario_id="S1",
        episode_no=1,
        sort_order=2,
        source_gold_ids=["W1"],
        domain="WORLD",
        operation="UPDATE",
        consolidation_status="SINGLE",
        target_ref=world_state_ref("RACE", "고블린", None, "체격"),
        matched_property_name="체격",
        proposed_setting_name="체격",
        proposed_value="평균 150cm다.",
        review_status="FINAL",
    )
    return GoldSnapshotV3(
        dataset_version="v3",
        name="io update",
        scenarios=[scenario],
        stage1=[source, world_source],
        stage2=[decision, world_decision],
    )
