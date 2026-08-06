import json
from pathlib import Path
from types import SimpleNamespace

from pydantic import ValidationError
import pytest

from evals.setting_extraction.assignment import maximum_weight_assignment
from evals.setting_extraction.evaluator import evaluate_predictions
from evals.setting_extraction.loader import load_gold_dataset, load_prediction_bundle
from evals.setting_extraction.models import (
    CharacterSettingSchemaSnapshot,
    GoldCandidate,
    GoldDataset,
    PredictionBundle,
    PredictionCandidate,
)
from evals.setting_extraction.semantic_judge import (
    DEFAULT_SEMANTIC_JUDGE_MODEL,
    OpenAISemanticValueJudge,
    SemanticJudgeBatchResult,
    SemanticJudgeCase,
    SemanticJudgeDecision,
)
from evals.setting_extraction.value_comparator import (
    ValueComparisonStatus,
    compare_candidate_value,
)


FIXTURE_DIR = Path(__file__).parent / "fixtures" / "setting_extraction_eval"


def test_evaluator_matches_alias_and_reports_semantic_pending() -> None:
    report = evaluate_predictions(
        load_gold_dataset(FIXTURE_DIR / "gold.json"),
        load_prediction_bundle([FIXTURE_DIR / "predictions.json"]),
    )

    assert report["metrics"]["detectionPrecision"] == 1.0
    assert report["metrics"]["detectionRecall"] == 1.0
    assert report["metrics"]["factMetricsComplete"] is False
    assert report["metrics"]["factF1"] is None
    assert report["counts"]["semanticPending"] == 1
    assert report["metrics"]["evidenceLocatableRate"] == 1.0
    assert report["metrics"]["goldEvidenceCoverageRate"] == 1.0


def test_evaluator_canonicalizes_schema_alias_without_duplicating_gold_aliases() -> None:
    predictions = _single_fact_predictions(
        entity_name="아이나르",
        match_status="UNRESOLVED",
    )
    predictions = predictions.model_copy(
        update={
            "episodes": [
                predictions.episodes[0].model_copy(
                    update={
                        "candidates": [
                            predictions.episodes[0]
                            .candidates[0]
                            .model_copy(update={"attribute_name": "profile.종족"})
                        ]
                    }
                )
            ]
        }
    )
    schemas = [
        CharacterSettingSchemaSnapshot.model_validate(
            {
                "schemaKey": "profile.species",
                "displayName": "종족",
                "attributePattern": None,
                "aliases": ["종족", "species", "race"],
                "valueType": "STRING",
            }
        )
    ]

    report = evaluate_predictions(
        _single_fact_gold(entity_name="아이나르"),
        predictions,
        setting_schemas=schemas,
    )

    assert report["counts"]["detectionMatches"] == 1
    assert report["episodes"][0]["matches"][0]["predicted_fact_key"] == "profile.종족"


def test_evaluator_uses_semantic_judge_for_fact_metrics() -> None:
    report = evaluate_predictions(
        load_gold_dataset(FIXTURE_DIR / "gold.json"),
        load_prediction_bundle([FIXTURE_DIR / "predictions.json"]),
        semantic_judge=AlwaysMatchJudge(),
    )

    assert report["metrics"]["factMetricsComplete"] is True
    assert report["metrics"]["factPrecision"] == 1.0
    assert report["metrics"]["factRecall"] == 1.0
    assert report["counts"]["judgeInputTokens"] == 100
    assert report["counts"]["judgeCachedInputTokens"] == 50
    assert report["counts"]["judgeOutputTokens"] == 20


def test_openai_semantic_judge_batches_cases_and_defaults_to_luna() -> None:
    gold = load_gold_dataset(FIXTURE_DIR / "gold.json")
    predictions = load_prediction_bundle([FIXTURE_DIR / "predictions.json"])
    client = RecordingJudgeClient()
    judge = OpenAISemanticValueJudge(client=client)

    result = judge.judge_many(
        [
            SemanticJudgeCase(
                gold=gold.episodes[0].candidates[1],
                prediction=predictions.episodes[0].candidates[1],
                source_text=gold.episodes[0].source_text,
            ),
            SemanticJudgeCase(
                gold=gold.episodes[0].candidates[1],
                prediction=predictions.episodes[0].candidates[1],
                source_text=gold.episodes[0].source_text,
            ),
        ]
    )

    assert len(client.requests) == 1
    assert client.requests[0]["model"] == DEFAULT_SEMANTIC_JUDGE_MODEL
    assert len(json.loads(client.requests[0]["user_prompt"])["cases"]) == 2
    assert len(result.decisions) == 2
    assert result.input_tokens == 120


def test_evaluator_counts_duplicate_prediction_as_false_positive() -> None:
    predictions = load_prediction_bundle([FIXTURE_DIR / "predictions.json"])
    level_prediction = predictions.episodes[0].candidates[0]
    duplicated = predictions.model_copy(
        update={
            "episodes": [
                predictions.episodes[0].model_copy(
                    update={
                        "candidates": [
                            *predictions.episodes[0].candidates,
                            level_prediction,
                        ]
                    }
                )
            ]
        }
    )

    report = evaluate_predictions(
        load_gold_dataset(FIXTURE_DIR / "gold.json"),
        duplicated,
        semantic_judge=AlwaysMatchJudge(),
    )

    assert report["counts"]["duplicates"] == 1
    assert report["metrics"]["detectionPrecision"] == 0.666667
    assert report["metrics"]["detectionRecall"] == 1.0


def test_evaluator_counts_do_not_extract_violation() -> None:
    predictions = load_prediction_bundle([FIXTURE_DIR / "predictions.json"])
    wrong_age = PredictionCandidate.model_validate(
        {
            "entity_name": "카엘",
            "attribute_name": "age",
            "attribute_value": "12",
            "value_type": "NUMBER",
            "value_json": {"value": 12},
            "evidence_spans": [{"quote": "카엘은 12레벨 검사다."}],
        }
    )
    with_wrong_age = predictions.model_copy(
        update={
            "episodes": [
                predictions.episodes[0].model_copy(
                    update={"candidates": [*predictions.episodes[0].candidates, wrong_age]}
                )
            ]
        }
    )

    report = evaluate_predictions(
        load_gold_dataset(FIXTURE_DIR / "gold.json"),
        with_wrong_age,
        semantic_judge=AlwaysMatchJudge(),
    )

    assert report["counts"]["hardNegativeViolations"] == 1
    assert report["metrics"]["hardNegativeViolationRate"] == 1.0


def test_number_comparison_uses_attribute_value_numeric_summary() -> None:
    gold = GoldCandidate.model_validate(
        {
            "decision": "EXTRACT",
            "importance": "MUST",
            "entityName": "비요른",
            "factKey": "stats.mental",
            "valueType": "NUMBER",
            "attributeValue": "36 (New +1)",
            "valueJson": {"value": 36},
            "evidenceQuotes": ["정신 수치가 36으로 올랐다."],
        }
    )
    prediction = PredictionCandidate.model_validate(
        {
            "entity_name": "비요른",
            "attribute_name": "stats.mental",
            "attribute_value": "정신 36",
            "value_type": "NUMBER",
            "value_json": {"name": "정신", "value": 36},
        }
    )

    comparison = compare_candidate_value(gold, prediction)

    assert comparison.status == ValueComparisonStatus.MATCH
    assert comparison.attribute_value_matched is True
    assert comparison.structured_value_matched is True


def test_evidence_failure_does_not_change_fact_correctness() -> None:
    predictions = load_prediction_bundle([FIXTURE_DIR / "predictions.json"])
    invalid_evidence = PredictionCandidate.model_validate(
        {
            **predictions.episodes[0].candidates[0].model_dump(),
            "evidence_spans": [{"quote": "원문에 없는 레벨 근거"}],
        }
    )
    predictions = predictions.model_copy(
        update={
            "episodes": [
                predictions.episodes[0].model_copy(
                    update={
                        "candidates": [
                            invalid_evidence,
                            predictions.episodes[0].candidates[1],
                        ]
                    }
                )
            ]
        }
    )

    report = evaluate_predictions(
        load_gold_dataset(FIXTURE_DIR / "gold.json"),
        predictions,
        semantic_judge=AlwaysMatchJudge(),
    )

    assert report["metrics"]["detectionRecall"] == 1.0
    assert report["metrics"]["factRecall"] == 1.0
    assert report["metrics"]["evidenceLocatableRate"] == 0.5
    assert report["episodes"][0]["matches"][0]["evidence_matched"] is False
    assert report["episodes"][0]["matches"][0]["fact_correct"] is True


def test_value_json_does_not_hide_attribute_value_mismatch() -> None:
    gold = GoldCandidate.model_validate(
        {
            "decision": "EXTRACT",
            "importance": "SHOULD",
            "entityName": "카엘",
            "factKey": "item.화염검",
            "valueType": "JSON",
            "attributeValue": "화염검을 장착",
            "valueJson": {"name": "화염검"},
            "evidenceQuotes": ["카엘은 화염검을 장착했다."],
        }
    )
    prediction = PredictionCandidate.model_validate(
        {
            "entityName": "카엘",
            "attributeName": "item.화염검",
            "attributeValue": "화염검을 버림",
            "valueType": "JSON",
            "valueJson": {"name": "화염검"},
        }
    )

    comparison = compare_candidate_value(gold, prediction)

    assert comparison.status == ValueComparisonStatus.SEMANTIC_JUDGE_REQUIRED
    assert comparison.structured_value_matched is True
    assert comparison.attribute_value_matched is None


def test_structured_value_mismatch_does_not_change_attribute_value_match() -> None:
    gold = GoldCandidate.model_validate(
        {
            "decision": "EXTRACT",
            "importance": "SHOULD",
            "entityName": "카엘",
            "factKey": "skill.화염구",
            "valueType": "JSON",
            "attributeValue": "화염구 Lv.3",
            "valueJson": {"name": "화염구", "level": 3},
            "evidenceQuotes": ["카엘은 화염구를 3레벨까지 익혔다."],
        }
    )
    prediction = PredictionCandidate.model_validate(
        {
            "entityName": "카엘",
            "attributeName": "skill.화염구",
            "attributeValue": "화염구 Lv.3",
            "valueType": "JSON",
            "valueJson": {"name": "화염구", "level": 4},
        }
    )

    comparison = compare_candidate_value(gold, prediction)

    assert comparison.status == ValueComparisonStatus.MATCH
    assert comparison.attribute_value_matched is True
    assert comparison.structured_value_matched is False


def test_semantic_attribute_judge_runs_even_when_structured_value_differs() -> None:
    gold = GoldDataset.model_validate(
        {
            "datasetVersion": "test-v1",
            "name": "서술값과 구조화 값 독립 판정",
            "episodes": [
                {
                    "episodeNo": 1,
                    "sourceText": "카엘은 고블린 덫을 밟아 오른쪽 발목을 다쳤다.",
                    "candidates": [
                        {
                            "decision": "EXTRACT",
                            "importance": "MUST",
                            "entityName": "카엘",
                            "factKey": "status.발목_부상",
                            "valueType": "JSON",
                            "attributeValue": "오른쪽 발목 부상",
                            "valueJson": {"name": "부상", "location": "오른쪽 발목"},
                            "evidenceQuotes": ["카엘은 고블린 덫을 밟아 오른쪽 발목을 다쳤다."],
                        }
                    ],
                }
            ],
        }
    )
    predictions = PredictionBundle.model_validate(
        {
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "entityName": "카엘",
                            "attributeName": "status.발목_부상",
                            "attributeValue": "고블린 덫으로 오른쪽 발목을 다침",
                            "valueType": "JSON",
                            "valueJson": {"name": "부상", "location": "왼쪽 발목"},
                            "evidenceSpans": [
                                {"quote": "카엘은 고블린 덫을 밟아 오른쪽 발목을 다쳤다."}
                            ],
                        }
                    ],
                }
            ]
        }
    )

    report = evaluate_predictions(gold, predictions, semantic_judge=AlwaysMatchJudge())

    match = report["episodes"][0]["matches"][0]
    assert report["counts"]["judgeInputTokens"] == 100
    assert report["metrics"]["factRecall"] == 1.0
    assert report["metrics"]["attributeValueAccuracy"] == 1.0
    assert report["metrics"]["structuredValueAccuracy"] == 0.0
    assert match["fact_correct"] is True
    assert match["structured_value_matched"] is False


def test_structured_value_accuracy_is_separate_from_fact_correctness() -> None:
    gold = GoldDataset.model_validate(
        {
            "datasetVersion": "test-v1",
            "name": "구조화 값 독립 지표",
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "decision": "EXTRACT",
                            "importance": "SHOULD",
                            "entityName": "카엘",
                            "factKey": "skill.화염구",
                            "valueType": "JSON",
                            "attributeValue": "화염구 Lv.3",
                            "valueJson": {"name": "화염구", "level": 3},
                            "evidenceQuotes": ["카엘은 화염구를 3레벨까지 익혔다."],
                        }
                    ],
                }
            ],
        }
    )
    predictions = PredictionBundle.model_validate(
        {
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "entityName": "카엘",
                            "attributeName": "skill.화염구",
                            "attributeValue": "화염구 Lv.3",
                            "valueType": "JSON",
                            "valueJson": {"name": "화염구", "level": 4},
                        }
                    ],
                }
            ]
        }
    )

    report = evaluate_predictions(gold, predictions)

    assert report["metrics"]["factRecall"] == 1.0
    assert report["metrics"]["attributeValueAccuracy"] == 1.0
    assert report["metrics"]["structuredValueAccuracy"] == 0.0
    assert report["episodes"][0]["matches"][0]["fact_correct"] is True


def test_matching_prefers_attribute_value_over_structured_value() -> None:
    gold = GoldDataset.model_validate(
        {
            "datasetVersion": "test-v1",
            "name": "표시값 중심 매칭",
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "decision": "EXTRACT",
                            "importance": "MUST",
                            "entityName": "카엘",
                            "factKey": "status.부상",
                            "valueType": "JSON",
                            "attributeValue": "오른쪽 발목 부상",
                            "valueJson": {"name": "부상", "location": "오른쪽 발목"},
                            "evidenceQuotes": ["카엘은 오른쪽 발목을 다쳤다."],
                        }
                    ],
                }
            ],
        }
    )
    predictions = PredictionBundle.model_validate(
        {
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "entityName": "카엘",
                            "attributeName": "status.부상",
                            "attributeValue": "오른쪽 발목 부상",
                            "valueType": "JSON",
                            "valueJson": {"name": "부상", "location": "왼쪽 발목"},
                        },
                        {
                            "entityName": "카엘",
                            "attributeName": "status.부상",
                            "attributeValue": "왼쪽 발목 부상",
                            "valueType": "JSON",
                            "valueJson": {"name": "부상", "location": "오른쪽 발목"},
                        },
                    ],
                }
            ]
        }
    )

    report = evaluate_predictions(gold, predictions)

    match = report["episodes"][0]["matches"][0]
    assert match["prediction_index"] == 0
    assert match["fact_correct"] is True
    assert match["structured_value_matched"] is False
    assert report["metrics"]["structuredValueAccuracy"] == 0.0


def test_empty_gold_value_json_is_excluded_from_structured_value_accuracy() -> None:
    gold = GoldDataset.model_validate(
        {
            "datasetVersion": "test-v1",
            "name": "구조화 값 생략",
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "decision": "EXTRACT",
                            "importance": "NICE",
                            "entityName": "카엘",
                            "factKey": "profile.occupation",
                            "valueType": "STRING",
                            "attributeValue": "검사",
                            "valueJson": {},
                            "evidenceQuotes": ["카엘은 검사다."],
                        }
                    ],
                }
            ],
        }
    )
    predictions = PredictionBundle.model_validate(
        {
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "entityName": "카엘",
                            "attributeName": "profile.occupation",
                            "attributeValue": "검사",
                            "valueType": "STRING",
                            "valueJson": {"value": "전사"},
                        }
                    ],
                }
            ]
        }
    )

    report = evaluate_predictions(gold, predictions)

    assert report["metrics"]["factRecall"] == 1.0
    assert report["counts"]["structuredValueEvaluated"] == 0
    assert report["metrics"]["structuredValueAccuracy"] is None


def test_gold_dataset_generates_internal_ids_without_extra_notion_column() -> None:
    gold = load_gold_dataset(FIXTURE_DIR / "gold.json")

    assert gold.episodes[0].candidates[0].gold_id == "episode-1:카엘:level:1"
    assert "gold_id" not in gold.episodes[0].candidates[0].model_dump()


def test_gold_loader_resolves_private_source_without_leaving_two_active_sources(
    tmp_path: Path,
) -> None:
    source_path = tmp_path / "01화.txt"
    source_path.write_text("카엘은 검사다.", encoding="utf-8")
    gold_path = tmp_path / "gold.json"
    gold_path.write_text(
        json.dumps(
            {
                "datasetVersion": "test-v1",
                "name": "원고 연결",
                "episodes": [{"episodeNo": 1, "sourceFile": "01화.txt"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    episode = load_gold_dataset(gold_path).episodes[0]

    assert episode.source_file is None
    assert episode.source_text == "카엘은 검사다."


def test_extract_gold_row_requires_explicit_red_scoring_columns() -> None:
    with pytest.raises(
        ValidationError,
        match="importance.*factKey.*valueType.*attributeValue.*valueJson.*evidenceQuotes",
    ):
        GoldCandidate.model_validate(
            {
                "decision": "EXTRACT",
                "entityName": "카엘",
            }
        )


def test_keyless_do_not_extract_row_is_loaded_but_reported_as_unscored() -> None:
    gold = GoldDataset.model_validate(
        {
            "datasetVersion": "test-v1",
            "name": "미판정 금지행",
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "decision": "DO_NOT_EXTRACT",
                            "entityName": "카엘",
                            "attributeValue": "이름 없는 무기",
                        }
                    ],
                }
            ],
        }
    )

    report = evaluate_predictions(gold, PredictionBundle())

    assert report["counts"]["goldDoNotExtract"] == 1
    assert report["counts"]["goldDoNotExtractUnscored"] == 1
    assert report["metrics"]["hardNegativeViolationRate"] is None
    assert len(report["episodes"][0]["unscoredHardNegativeGoldIds"]) == 1


def test_review_required_prediction_is_excluded_when_identity_is_known() -> None:
    gold = GoldDataset.model_validate(
        {
            "datasetVersion": "test-v1",
            "name": "미확정 후보 제외",
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "decision": "REVIEW_REQUIRED",
                            "entityName": "카엘",
                            "factKey": "status.정체_불명",
                        }
                    ],
                }
            ],
        }
    )
    predictions = PredictionBundle.model_validate(
        {
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "entityName": "카엘",
                            "attributeName": "status.정체_불명",
                            "attributeValue": "정체를 알 수 없음",
                            "valueType": "JSON",
                            "valueJson": {"name": "정체 불명"},
                        }
                    ],
                }
            ]
        }
    )

    report = evaluate_predictions(gold, predictions)

    assert report["counts"]["predictionTotal"] == 1
    assert report["counts"]["predictions"] == 0
    assert report["counts"]["reviewExcludedPredictions"] == 1
    assert report["metrics"]["detectionPrecision"] is None
    assert report["episodes"][0]["unmatchedPredictionIndexes"] == []
    assert report["episodes"][0]["reviewExcludedPredictionIndexes"] == [0]


def test_debug_prediction_uses_operational_matched_character_name_for_detection(
    tmp_path: Path,
) -> None:
    matched_character_id = "00000000-0000-0000-0000-000000000101"
    prediction_path = tmp_path / "episode-1-result.json"
    prediction_path.write_text(
        json.dumps(
            {
                "summary": {"episodeNo": 1},
                "knownCharacters": [
                    {
                        "characterId": matched_character_id,
                        "name": "비요른 얀델",
                    }
                ],
                "settingCandidates": [
                    {
                        "entity_name": "비요른",
                        "matched_character_id": matched_character_id,
                        "match_status": "MATCHED",
                        "attribute_name": "profile.species",
                        "attribute_value": "바바리안",
                        "value_type": "STRING",
                        "value_json": {"value": "바바리안"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    predictions = load_prediction_bundle([prediction_path])

    report = evaluate_predictions(
        _single_fact_gold(entity_name="비요른 얀델"),
        predictions,
    )

    assert predictions.episodes[0].candidates[0].entity_name == "비요른"
    assert predictions.episodes[0].candidates[0].evaluation_entity_name == "비요른 얀델"
    assert report["counts"]["detectionMatches"] == 1


def test_character_discovery_candidate_is_reported_but_not_scored_as_setting(
    tmp_path: Path,
) -> None:
    prediction_path = tmp_path / "episode-1-result.json"
    prediction_path.write_text(
        json.dumps(
            {
                "summary": {"episodeNo": 1},
                "knownCharacters": [],
                "settingCandidates": [
                    {
                        "candidateKind": "CHARACTER_DISCOVERY",
                        "entityName": "아이나르",
                        "rawEntityMention": "프넬린의 두 번째 딸 아이나르",
                        "attributeName": None,
                        "attributeValue": None,
                        "valueType": None,
                        "valueJson": None,
                    },
                    {
                        "candidate_kind": "SETTING",
                        "entity_name": "아이나르",
                        "match_status": "UNRESOLVED",
                        "attribute_name": "profile.species",
                        "attribute_value": "바바리안",
                        "value_type": "STRING",
                        "value_json": {"value": "바바리안"},
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    predictions = load_prediction_bundle([prediction_path])
    report = evaluate_predictions(_single_fact_gold(entity_name="아이나르"), predictions)

    assert len(predictions.episodes[0].candidates) == 1
    assert predictions.episodes[0].character_discovery_excluded_count == 1
    assert report["counts"]["characterDiscoveryExcluded"] == 1
    assert report["counts"]["predictionTotal"] == 1
    assert report["counts"]["detectionMatches"] == 1


def test_unresolved_new_character_keeps_exact_name_detection() -> None:
    predictions = _single_fact_predictions(
        entity_name="아이나르",
        match_status="UNRESOLVED",
    )

    report = evaluate_predictions(
        _single_fact_gold(entity_name="아이나르"),
        predictions,
    )

    assert report["counts"]["detectionMatches"] == 1


def test_ambiguous_character_is_not_accepted_as_detection() -> None:
    predictions = _single_fact_predictions(
        entity_name="비요른 얀델",
        match_status="AMBIGUOUS",
    )

    report = evaluate_predictions(
        _single_fact_gold(entity_name="비요른 얀델"),
        predictions,
    )

    assert report["counts"]["detectionMatches"] == 0
    assert report["episodes"][0]["unmatchedPredictionIndexes"] == [0]


def test_unknown_subject_is_reported_as_separate_recoverable_failure() -> None:
    predictions = _single_fact_predictions(
        entity_name="미상",
        match_status="AMBIGUOUS",
    )

    report = evaluate_predictions(
        _single_fact_gold(entity_name="아이나르"),
        predictions,
    )

    assert report["counts"]["detectionMatches"] == 0
    assert report["metrics"]["detectionRecall"] == 0.0
    assert report["counts"]["unknownSubjectPredictions"] == 1
    assert report["counts"]["subjectOnlyFailures"] == 1
    assert report["metrics"]["unknownSubjectPredictionRate"] == 1.0
    assert report["metrics"]["subjectOnlyFailureRate"] == 1.0
    assert report["metrics"]["unknownSubjectRecoverableRate"] == 1.0
    assert report["episodes"][0]["subjectResolutionDiagnostics"] == {
        "unknownPredictionIndexes": [0],
        "subjectOnlyMatches": [
            {
                "predictionIndex": 0,
                "goldId": "episode-1:아이나르:profile.species:1",
            }
        ],
        "ambiguousMatches": [],
        "pendingMatches": [],
        "unmatchedIndexes": [],
    }


def test_unknown_subject_is_ambiguous_when_multiple_characters_share_fact() -> None:
    gold = GoldDataset.model_validate(
        {
            "datasetVersion": "test-v1",
            "name": "미상 주체 모호성",
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "decision": "EXTRACT",
                            "importance": "MUST",
                            "entityName": entity_name,
                            "factKey": "profile.species",
                            "valueType": "STRING",
                            "attributeValue": "바바리안",
                            "valueJson": {"value": "바바리안"},
                            "evidenceQuotes": [f"{entity_name}은 바바리안이다."],
                        }
                        for entity_name in ("아이나르", "비요른")
                    ],
                }
            ],
        }
    )
    predictions = _single_fact_predictions(
        entity_name="미상",
        match_status="AMBIGUOUS",
    )

    report = evaluate_predictions(gold, predictions)

    assert report["counts"]["subjectOnlyFailures"] == 0
    assert report["counts"]["ambiguousUnknownSubjectPredictions"] == 1
    assert report["metrics"]["unknownSubjectRecoverableRate"] == 0.0
    assert report["metrics"]["ambiguousUnknownSubjectRate"] == 1.0
    diagnostic = report["episodes"][0]["subjectResolutionDiagnostics"]
    assert diagnostic["subjectOnlyMatches"] == []
    assert diagnostic["ambiguousMatches"][0]["predictionIndex"] == 0
    assert len(diagnostic["ambiguousMatches"][0]["candidateGoldIds"]) == 2


def test_duplicate_unknown_subjects_are_not_both_counted_as_recoverable() -> None:
    predictions = _single_fact_predictions(
        entity_name="미상",
        match_status="AMBIGUOUS",
    )
    prediction = predictions.episodes[0].candidates[0]
    predictions = predictions.model_copy(
        update={
            "episodes": [
                predictions.episodes[0].model_copy(update={"candidates": [prediction, prediction]})
            ]
        }
    )

    report = evaluate_predictions(
        _single_fact_gold(entity_name="아이나르"),
        predictions,
    )

    assert report["counts"]["unknownSubjectPredictions"] == 2
    assert report["counts"]["subjectOnlyFailures"] == 0
    assert report["counts"]["ambiguousUnknownSubjectPredictions"] == 2
    assert report["metrics"]["unknownSubjectRecoverableRate"] == 0.0
    assert report["metrics"]["ambiguousUnknownSubjectRate"] == 1.0


def test_unknown_subject_with_wrong_type_is_reported_unmatched() -> None:
    predictions = _single_fact_predictions(
        entity_name="미상",
        match_status="AMBIGUOUS",
    )
    predictions = predictions.model_copy(
        update={
            "episodes": [
                predictions.episodes[0].model_copy(
                    update={
                        "candidates": [
                            predictions.episodes[0]
                            .candidates[0]
                            .model_copy(
                                update={
                                    "attribute_value": "1",
                                    "value_type": "NUMBER",
                                    "value_json": {"value": 1},
                                }
                            )
                        ]
                    }
                )
            ]
        }
    )

    report = evaluate_predictions(
        _single_fact_gold(entity_name="아이나르"),
        predictions,
    )

    assert report["counts"]["subjectOnlyFailures"] == 0
    assert report["counts"]["unmatchedUnknownSubjectPredictions"] == 1
    diagnostic = report["episodes"][0]["subjectResolutionDiagnostics"]
    assert diagnostic["unmatchedIndexes"] == [0]


def test_unknown_subject_semantic_value_is_reported_pending_without_extra_judge_call() -> None:
    gold = GoldDataset.model_validate(
        {
            "datasetVersion": "test-v1",
            "name": "미상 주체 의미 판정 보류",
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "decision": "EXTRACT",
                            "importance": "SHOULD",
                            "entityName": "아이나르",
                            "factKey": "status.발목_부상",
                            "valueType": "JSON",
                            "attributeValue": "오른쪽 발목 부상",
                            "valueJson": {"name": "발목 부상"},
                            "evidenceQuotes": ["아이나르는 오른쪽 발목을 다쳤다."],
                        }
                    ],
                }
            ],
        }
    )
    predictions = PredictionBundle.model_validate(
        {
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "entityName": "미상",
                            "matchStatus": "AMBIGUOUS",
                            "attributeName": "status.발목_부상",
                            "attributeValue": "고블린 덫 때문에 오른쪽 발목을 다침",
                            "valueType": "JSON",
                            "valueJson": {"name": "발목 부상"},
                        }
                    ],
                }
            ]
        }
    )

    report = evaluate_predictions(gold, predictions, semantic_judge=FailOnCallJudge())

    assert report["counts"]["subjectOnlyFailures"] == 0
    assert report["counts"]["pendingUnknownSubjectPredictions"] == 1
    assert report["metrics"]["pendingUnknownSubjectRate"] == 1.0
    diagnostic = report["episodes"][0]["subjectResolutionDiagnostics"]
    assert diagnostic["pendingMatches"][0]["predictionIndex"] == 0


def test_matched_character_with_raw_unknown_name_is_not_subject_failure() -> None:
    predictions = _single_fact_predictions(
        entity_name="미상",
        match_status="MATCHED",
    )
    predictions = predictions.model_copy(
        update={
            "episodes": [
                predictions.episodes[0].model_copy(
                    update={
                        "candidates": [
                            predictions.episodes[0]
                            .candidates[0]
                            .model_copy(update={"matched_character_name": "아이나르"})
                        ]
                    }
                )
            ]
        }
    )

    report = evaluate_predictions(
        _single_fact_gold(entity_name="아이나르"),
        predictions,
    )

    assert report["counts"]["detectionMatches"] == 1
    assert report["counts"]["unknownSubjectPredictions"] == 0
    assert report["metrics"]["unknownSubjectPredictionRate"] == 0.0


def test_debug_prediction_rejects_unknown_matched_character_id(
    tmp_path: Path,
) -> None:
    prediction_path = tmp_path / "invalid-result.json"
    prediction_path.write_text(
        json.dumps(
            {
                "summary": {"episodeNo": 1},
                "knownCharacters": [],
                "settingCandidates": [
                    {
                        "entity_name": "비요른",
                        "matched_character_id": "unknown-character-id",
                        "match_status": "MATCHED",
                        "attribute_name": "profile.species",
                        "attribute_value": "바바리안",
                        "value_type": "STRING",
                        "value_json": {"value": "바바리안"},
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="unknown character ID"):
        load_prediction_bundle([prediction_path])


def test_maximum_weight_assignment_chooses_global_optimum() -> None:
    assert maximum_weight_assignment([[9, 8], [8, 0]]) == [(0, 1), (1, 0)]


def _single_fact_gold(entity_name: str) -> GoldDataset:
    return GoldDataset.model_validate(
        {
            "datasetVersion": "test-v1",
            "name": "캐릭터 이름 해소 평가",
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "decision": "EXTRACT",
                            "importance": "MUST",
                            "entityName": entity_name,
                            "factKey": "profile.species",
                            "valueType": "STRING",
                            "attributeValue": "바바리안",
                            "valueJson": {"value": "바바리안"},
                            "evidenceQuotes": ["나는 바바리안이다."],
                        }
                    ],
                }
            ],
        }
    )


def _single_fact_predictions(
    entity_name: str,
    match_status: str,
) -> PredictionBundle:
    return PredictionBundle.model_validate(
        {
            "episodes": [
                {
                    "episodeNo": 1,
                    "candidates": [
                        {
                            "entityName": entity_name,
                            "matchStatus": match_status,
                            "attributeName": "profile.species",
                            "attributeValue": "바바리안",
                            "valueType": "STRING",
                            "valueJson": {"value": "바바리안"},
                        }
                    ],
                }
            ]
        }
    )


class AlwaysMatchJudge:
    def judge_many(self, cases) -> SemanticJudgeBatchResult:
        return SemanticJudgeBatchResult(
            decisions=tuple(
                SemanticJudgeDecision(
                    core_meaning_covered=True,
                    supported_by_evidence=True,
                    contradiction=False,
                    unsupported_detail=False,
                    reason="같은 의미이며 원문이 뒷받침합니다.",
                )
                for _ in cases
            ),
            input_tokens=100,
            cached_input_tokens=50,
            output_tokens=20,
        )


class FailOnCallJudge:
    def judge_many(self, cases) -> SemanticJudgeBatchResult:
        raise AssertionError(f"Subject diagnostics must not call semantic judge: {cases}")


class RecordingJudgeClient:
    def __init__(self) -> None:
        self.requests = []

    def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        case_count = len(json.loads(kwargs["user_prompt"])["cases"])
        return SimpleNamespace(
            text=json.dumps(
                {
                    "results": [
                        {
                            "caseId": case_id,
                            "core_meaning_covered": True,
                            "supported_by_evidence": True,
                            "contradiction": False,
                            "unsupported_detail": False,
                            "reason": "같은 의미",
                        }
                        for case_id in range(case_count)
                    ]
                },
                ensure_ascii=False,
            ),
            input_token_count=120,
            cached_input_token_count=60,
            output_token_count=40,
        )
