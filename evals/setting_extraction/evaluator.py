from dataclasses import asdict, dataclass
from typing import Any

from app.domain.enums import SettingCandidateMatchStatus
from evals.setting_extraction.assignment import maximum_weight_assignment
from evals.setting_extraction.evidence import EvidenceEvaluation, evaluate_evidence
from evals.setting_extraction.models import (
    CharacterSettingSchemaSnapshot,
    GoldCandidate,
    GoldDataset,
    GoldDecision,
    PredictionBundle,
    PredictionCandidate,
)
from evals.setting_extraction.normalization import normalize_fact_key, normalize_text
from evals.setting_extraction.semantic_judge import SemanticJudgeCase, SemanticValueJudge
from evals.setting_extraction.schema_normalizer import canonicalize_prediction_fact_keys
from evals.setting_extraction.value_comparator import (
    ValueComparison,
    ValueComparisonStatus,
    compare_candidate_value,
)


UNKNOWN_SUBJECT_NAME = "미상"


@dataclass(frozen=True)
class CandidateMatchResult:
    gold_id: str
    prediction_index: int
    entity_name: str
    fact_key: str
    predicted_fact_key: str
    value_status: str
    value_reason: str
    value_type_matched: bool
    structured_value_matched: bool | None
    attribute_value_matched: bool | None
    value_matched: bool | None
    evidence_matched: bool
    fact_correct: bool | None
    evidence_quote_count: int
    locatable_evidence_quote_count: int
    covered_gold_quote_count: int
    semantic_judge_reason: str | None = None


def evaluate_predictions(
    gold_dataset: GoldDataset,
    prediction_bundle: PredictionBundle,
    semantic_judge: SemanticValueJudge | None = None,
    setting_schemas: list[CharacterSettingSchemaSnapshot] | None = None,
) -> dict[str, Any]:
    if setting_schemas is not None:
        prediction_bundle = canonicalize_prediction_fact_keys(
            prediction_bundle,
            setting_schemas,
        )
    gold_episode_by_no = {episode.episode_no: episode for episode in gold_dataset.episodes}
    prediction_episode_by_no = {
        episode.episode_no: episode for episode in prediction_bundle.episodes
    }
    episode_numbers = sorted(set(gold_episode_by_no) | set(prediction_episode_by_no))

    aggregate = _empty_aggregate()
    episode_reports = []
    for episode_no in episode_numbers:
        gold_episode = gold_episode_by_no.get(episode_no)
        prediction_episode = prediction_episode_by_no.get(episode_no)
        gold_candidates = gold_episode.candidates if gold_episode else []
        predictions = prediction_episode.candidates if prediction_episode else []
        source_text = gold_episode.source_text if gold_episode else None
        episode_report = _evaluate_episode(
            episode_no=episode_no,
            gold_candidates=gold_candidates,
            predictions=predictions,
            character_discovery_excluded_count=(
                prediction_episode.character_discovery_excluded_count
                if prediction_episode
                else 0
            ),
            source_text=source_text,
            semantic_judge=semantic_judge,
        )
        _merge_aggregate(aggregate, episode_report["counts"])
        episode_reports.append(episode_report)

    metrics = _build_metrics(aggregate)
    return {
        "dataset": {
            "name": gold_dataset.name,
            "version": gold_dataset.dataset_version,
            "episodeCount": len(gold_dataset.episodes),
        },
        "metrics": metrics,
        "counts": aggregate,
        "episodes": episode_reports,
    }


def _evaluate_episode(
    episode_no: int,
    gold_candidates: list[GoldCandidate],
    predictions: list[PredictionCandidate],
    character_discovery_excluded_count: int,
    source_text: str | None,
    semantic_judge: SemanticValueJudge | None,
) -> dict[str, Any]:
    # REVIEW_REQUIRED는 정답 작성 중인 행이므로 분모에서도 제외한다.
    extract_gold = [item for item in gold_candidates if item.decision == GoldDecision.EXTRACT]
    hard_negative_gold = [
        item for item in gold_candidates if item.decision == GoldDecision.DO_NOT_EXTRACT
    ]
    scorable_hard_negative_gold = [
        item for item in hard_negative_gold if item.is_scorable_hard_negative
    ]
    unscorable_hard_negative_gold = [
        item for item in hard_negative_gold if not item.is_scorable_hard_negative
    ]
    review_gold = [
        item for item in gold_candidates if item.decision == GoldDecision.REVIEW_REQUIRED
    ]

    weights = [
        [_candidate_match_weight(gold, prediction) for prediction in predictions]
        for gold in extract_gold
    ]
    # 한 예측을 여러 정답에 중복 배정하지 않도록 회차 전체에서 최적 1:1 매칭한다.
    assignments = maximum_weight_assignment(weights)
    matched_gold_indexes = {gold_index for gold_index, _ in assignments}
    matched_prediction_indexes = {prediction_index for _, prediction_index in assignments}
    subject_diagnostics = _diagnose_unknown_subjects(
        extract_gold,
        predictions,
        matched_gold_indexes,
        matched_prediction_indexes,
    )

    match_results: list[CandidateMatchResult] = []
    semantic_pending_count = 0
    fact_correct_count = 0
    value_type_correct_count = 0
    structured_value_evaluated_count = 0
    structured_value_correct_count = 0
    attribute_value_correct_count = 0
    evidence_quote_count = 0
    locatable_evidence_quote_count = 0
    matched_with_prediction_evidence_count = 0
    matched_with_gold_evidence_count = 0
    matched_with_gold_evidence_covered_count = 0
    fact_correct_weight = 0
    judge_input_tokens = 0
    judge_cached_input_tokens = 0
    judge_output_tokens = 0

    comparisons = {
        (gold_index, prediction_index): compare_candidate_value(
            extract_gold[gold_index],
            predictions[prediction_index],
        )
        for gold_index, prediction_index in assignments
    }
    semantic_assignment_keys = [
        assignment
        for assignment in assignments
        if comparisons[assignment].status == ValueComparisonStatus.SEMANTIC_JUDGE_REQUIRED
    ]
    semantic_decision_by_assignment = {}
    if semantic_judge is not None and semantic_assignment_keys:
        # 서로 독립적인 서술형 판정만 회차 안에서 묶어 호출 횟수를 줄인다.
        judge_batch = semantic_judge.judge_many(
            [
                SemanticJudgeCase(
                    gold=extract_gold[gold_index],
                    prediction=predictions[prediction_index],
                    source_text=source_text,
                )
                for gold_index, prediction_index in semantic_assignment_keys
            ]
        )
        if len(judge_batch.decisions) != len(semantic_assignment_keys):
            raise ValueError("Semantic judge returned a different number of decisions.")
        semantic_decision_by_assignment = dict(
            zip(semantic_assignment_keys, judge_batch.decisions, strict=True)
        )
        judge_input_tokens = judge_batch.input_tokens
        judge_cached_input_tokens = judge_batch.cached_input_tokens
        judge_output_tokens = judge_batch.output_tokens

    for gold_index, prediction_index in assignments:
        gold = extract_gold[gold_index]
        prediction = predictions[prediction_index]
        comparison = comparisons[(gold_index, prediction_index)]
        value_matched: bool | None
        attribute_value_matched = comparison.attribute_value_matched
        semantic_reason = None
        if comparison.status == ValueComparisonStatus.SEMANTIC_JUDGE_REQUIRED:
            if semantic_judge is None:
                # Judge 미사용은 오답이 아니라 미판정이다. 종단 간 지표도 완결하지 않는다.
                semantic_pending_count += 1
                value_matched = None
            else:
                judge_decision = semantic_decision_by_assignment[(gold_index, prediction_index)]
                value_matched = judge_decision.matched
                attribute_value_matched = value_matched
                semantic_reason = judge_decision.reason
        else:
            value_matched = comparison.status == ValueComparisonStatus.MATCH

        evidence = evaluate_evidence(gold, prediction, source_text)
        evidence_matched = _evidence_is_acceptable(gold, evidence, source_text)
        # 원문 근거는 별도 품질 지표이며 Fact의 key·type·value 정답 여부를 뒤집지 않는다.
        fact_correct = value_matched
        if fact_correct:
            fact_correct_count += 1
            fact_correct_weight += _importance_weight(gold)
        if comparison.value_type_matched:
            value_type_correct_count += 1
        if comparison.structured_value_matched is not None:
            structured_value_evaluated_count += 1
            if comparison.structured_value_matched:
                structured_value_correct_count += 1
        if attribute_value_matched:
            attribute_value_correct_count += 1

        if source_text is not None:
            evidence_quote_count += evidence.quote_count
            locatable_evidence_quote_count += evidence.locatable_quote_count
        if evidence.quote_count > 0:
            matched_with_prediction_evidence_count += 1
        if evidence.gold_quote_count > 0:
            matched_with_gold_evidence_count += 1
            if evidence.has_gold_quote_coverage:
                matched_with_gold_evidence_covered_count += 1
        match_results.append(
            _build_match_result(
                gold,
                prediction,
                prediction_index,
                comparison,
                evidence,
                value_matched,
                attribute_value_matched,
                evidence_matched,
                fact_correct,
                semantic_reason,
            )
        )

    unmatched_prediction_indexes = [
        index for index in range(len(predictions)) if index not in matched_prediction_indexes
    ]
    # key까지 확정된 REVIEW_REQUIRED 행과 같은 예측은 아직 정답·오답을 판정할 수 없으므로
    # Precision 분모와 오탐 목록에서 제외한다. key가 없는 미확정 행은 임의 예측을 숨기지 않는다.
    review_excluded_prediction_indexes = [
        index
        for index in unmatched_prediction_indexes
        if any(_identity_matches(gold, predictions[index]) for gold in review_gold)
    ]
    scored_unmatched_prediction_indexes = [
        index
        for index in unmatched_prediction_indexes
        if index not in review_excluded_prediction_indexes
    ]
    hard_negative_violations = _match_hard_negatives(
        scorable_hard_negative_gold,
        predictions,
        scored_unmatched_prediction_indexes,
    )
    # 이미 정답에 배정된 예측을 오탐이나 중복으로 다시 집계하지 않는다.
    duplicate_prediction_indexes = [
        index
        for index in scored_unmatched_prediction_indexes
        if any(_identity_matches(gold, predictions[index]) for gold in extract_gold)
    ]
    matched_weight = sum(_importance_weight(extract_gold[index]) for index in matched_gold_indexes)
    total_weight = sum(_importance_weight(gold) for gold in extract_gold)

    counts = {
        "goldExtract": len(extract_gold),
        "goldDoNotExtract": len(hard_negative_gold),
        "goldDoNotExtractScored": len(scorable_hard_negative_gold),
        "goldDoNotExtractUnscored": len(unscorable_hard_negative_gold),
        "goldReviewExcluded": len(review_gold),
        "characterDiscoveryExcluded": character_discovery_excluded_count,
        "predictionTotal": len(predictions),
        "predictions": len(predictions) - len(review_excluded_prediction_indexes),
        "reviewExcludedPredictions": len(review_excluded_prediction_indexes),
        "detectionMatches": len(assignments),
        "factCorrect": fact_correct_count,
        "semanticPending": semantic_pending_count,
        "valueTypeCorrect": value_type_correct_count,
        "structuredValueEvaluated": structured_value_evaluated_count,
        "structuredValueCorrect": structured_value_correct_count,
        "attributeValueCorrect": attribute_value_correct_count,
        "hardNegativeViolations": len(hard_negative_violations),
        "duplicates": len(duplicate_prediction_indexes),
        "weightedGold": total_weight,
        "weightedDetectionMatches": matched_weight,
        "weightedFactCorrect": fact_correct_weight,
        "evidenceQuotes": evidence_quote_count,
        "locatableEvidenceQuotes": locatable_evidence_quote_count,
        "matchedWithPredictionEvidence": matched_with_prediction_evidence_count,
        "matchedWithGoldEvidence": matched_with_gold_evidence_count,
        "matchedWithCoveredGoldEvidence": matched_with_gold_evidence_covered_count,
        "judgeInputTokens": judge_input_tokens,
        "judgeCachedInputTokens": judge_cached_input_tokens,
        "judgeOutputTokens": judge_output_tokens,
        "unknownSubjectPredictions": len(subject_diagnostics["unknownPredictionIndexes"]),
        "subjectOnlyFailures": len(subject_diagnostics["subjectOnlyMatches"]),
        "ambiguousUnknownSubjectPredictions": len(subject_diagnostics["ambiguousMatches"]),
        "pendingUnknownSubjectPredictions": len(subject_diagnostics["pendingMatches"]),
        "unmatchedUnknownSubjectPredictions": len(subject_diagnostics["unmatchedIndexes"]),
    }
    return {
        "episodeNo": episode_no,
        "counts": counts,
        "matches": [asdict(result) for result in match_results],
        "unmatchedGoldIds": [
            gold.gold_id
            for index, gold in enumerate(extract_gold)
            if index not in matched_gold_indexes
        ],
        "unmatchedPredictionIndexes": scored_unmatched_prediction_indexes,
        "reviewExcludedPredictionIndexes": review_excluded_prediction_indexes,
        "duplicatePredictionIndexes": duplicate_prediction_indexes,
        "hardNegativeViolations": hard_negative_violations,
        "unscoredHardNegativeGoldIds": [
            candidate.gold_id for candidate in unscorable_hard_negative_gold
        ],
        "subjectResolutionDiagnostics": subject_diagnostics,
    }


def _diagnose_unknown_subjects(
    extract_gold: list[GoldCandidate],
    predictions: list[PredictionCandidate],
    matched_gold_indexes: set[int],
    matched_prediction_indexes: set[int],
) -> dict[str, Any]:
    """주체만 해소됐다면 정답이 될 수 있는 `미상` 후보를 본 평가와 분리해 찾는다."""

    unknown_prediction_indexes = [
        index for index, prediction in enumerate(predictions) if _has_unknown_subject(prediction)
    ]
    unmatched_unknown_indexes = [
        index for index in unknown_prediction_indexes if index not in matched_prediction_indexes
    ]
    unmatched_gold_indexes = [
        index for index in range(len(extract_gold)) if index not in matched_gold_indexes
    ]

    deterministic_gold_indexes_by_prediction: dict[int, list[int]] = {}
    pending_gold_indexes_by_prediction: dict[int, list[int]] = {}
    for prediction_index in unmatched_unknown_indexes:
        prediction = predictions[prediction_index]
        deterministic_matches = []
        pending_matches = []
        for gold_index in unmatched_gold_indexes:
            gold = extract_gold[gold_index]
            if not _fact_key_matches(gold, prediction):
                continue
            comparison = compare_candidate_value(gold, prediction)
            if comparison.status == ValueComparisonStatus.MATCH:
                deterministic_matches.append(gold_index)
            elif comparison.status == ValueComparisonStatus.SEMANTIC_JUDGE_REQUIRED:
                # 진단 지표가 평가 비용을 몰래 늘리지 않도록 추가 LLM 호출은 하지 않는다.
                pending_matches.append(gold_index)
        deterministic_gold_indexes_by_prediction[prediction_index] = deterministic_matches
        pending_gold_indexes_by_prediction[prediction_index] = pending_matches

    # 같은 정답에 여러 `미상` 예측이 걸리면 어느 예측이 정답인지 확정할 수 없다.
    prediction_count_by_gold_index: dict[int, int] = {}
    for gold_indexes in deterministic_gold_indexes_by_prediction.values():
        for gold_index in gold_indexes:
            prediction_count_by_gold_index[gold_index] = (
                prediction_count_by_gold_index.get(gold_index, 0) + 1
            )

    subject_only_matches = []
    ambiguous_matches = []
    pending_matches = []
    unmatched_indexes = []
    for prediction_index in unmatched_unknown_indexes:
        gold_indexes = deterministic_gold_indexes_by_prediction[prediction_index]
        if len(gold_indexes) == 1 and prediction_count_by_gold_index[gold_indexes[0]] == 1:
            subject_only_matches.append(
                {
                    "predictionIndex": prediction_index,
                    "goldId": extract_gold[gold_indexes[0]].gold_id,
                }
            )
            continue
        if gold_indexes:
            ambiguous_matches.append(
                {
                    "predictionIndex": prediction_index,
                    "candidateGoldIds": [extract_gold[index].gold_id for index in gold_indexes],
                }
            )
            continue

        semantic_pending_indexes = pending_gold_indexes_by_prediction[prediction_index]
        if semantic_pending_indexes:
            pending_matches.append(
                {
                    "predictionIndex": prediction_index,
                    "candidateGoldIds": [
                        extract_gold[index].gold_id for index in semantic_pending_indexes
                    ],
                }
            )
            continue
        unmatched_indexes.append(prediction_index)

    return {
        "unknownPredictionIndexes": unknown_prediction_indexes,
        "subjectOnlyMatches": subject_only_matches,
        "ambiguousMatches": ambiguous_matches,
        "pendingMatches": pending_matches,
        "unmatchedIndexes": unmatched_indexes,
    }


def _has_unknown_subject(prediction: PredictionCandidate) -> bool:
    # 운영 이름 해소가 끝난 MATCHED 후보는 추출 이름이 미상이었더라도 실패로 집계하지 않는다.
    return prediction.match_status != SettingCandidateMatchStatus.MATCHED and normalize_text(
        prediction.entity_name
    ) == normalize_text(UNKNOWN_SUBJECT_NAME)


def _fact_key_matches(gold: GoldCandidate, prediction: PredictionCandidate) -> bool:
    predicted_key = normalize_fact_key(prediction.evaluation_fact_key)
    return predicted_key in {
        normalize_fact_key(accepted_key) for accepted_key in gold.accepted_fact_keys
    }


def _candidate_match_weight(
    gold: GoldCandidate,
    prediction: PredictionCandidate,
) -> int:
    weight = _identity_match_weight(gold, prediction)
    if weight == 0:
        return 0

    # 값과 근거는 identity가 같은 여러 예측 중 하나를 고르는 tie-breaker일 뿐이다.
    comparison = compare_candidate_value(gold, prediction)
    if comparison.status == ValueComparisonStatus.MATCH:
        weight += 100
    elif comparison.status == ValueComparisonStatus.SEMANTIC_JUDGE_REQUIRED:
        weight += 50
    if comparison.value_type_matched:
        weight += 20
    evidence = evaluate_evidence(gold, prediction, None)
    if evidence.has_gold_quote_coverage:
        weight += 5
    return weight


def _identity_matches(gold: GoldCandidate, prediction: PredictionCandidate) -> bool:
    return _identity_match_weight(gold, prediction) > 0


def _identity_match_weight(
    gold: GoldCandidate,
    prediction: PredictionCandidate,
) -> int:
    # 실제 분석에서 MATCHED로 확정된 기존 캐릭터는 ID로 찾은 대표 이름을 사용한다.
    # AMBIGUOUS는 자동 정답으로 인정하지 않고, UNRESOLVED는 신규 후보일 수 있어
    # 추출된 entityName을 기존 exact 규칙으로 비교한다.
    if normalize_text(gold.entity_name) != normalize_text(prediction.evaluation_entity_name):
        return 0
    predicted_key = normalize_fact_key(prediction.evaluation_fact_key)
    accepted_keys = [normalize_fact_key(key) for key in gold.accepted_fact_keys]
    if predicted_key not in accepted_keys:
        return 0
    canonical_key = normalize_fact_key(gold.fact_key) if gold.fact_key else None
    return 1000 if predicted_key == canonical_key else 900


def _match_hard_negatives(
    hard_negatives: list[GoldCandidate],
    predictions: list[PredictionCandidate],
    prediction_indexes: list[int],
) -> list[dict[str, Any]]:
    if not hard_negatives or not prediction_indexes:
        return []
    weights = []
    for gold in hard_negatives:
        row = []
        for prediction_index in prediction_indexes:
            prediction = predictions[prediction_index]
            # DO_NOT_EXTRACT는 해당 key를 생성한 것 자체가 위반이므로 값 비교를 요구하지 않는다.
            row.append(_identity_match_weight(gold, prediction))
        weights.append(row)
    return [
        {
            "goldId": hard_negatives[gold_index].gold_id,
            "predictionIndex": prediction_indexes[prediction_column],
        }
        for gold_index, prediction_column in maximum_weight_assignment(weights)
    ]


def _build_match_result(
    gold: GoldCandidate,
    prediction: PredictionCandidate,
    prediction_index: int,
    comparison: ValueComparison,
    evidence: EvidenceEvaluation,
    value_matched: bool | None,
    attribute_value_matched: bool | None,
    evidence_matched: bool,
    fact_correct: bool | None,
    semantic_reason: str | None,
) -> CandidateMatchResult:
    return CandidateMatchResult(
        gold_id=gold.gold_id,
        prediction_index=prediction_index,
        entity_name=gold.entity_name,
        fact_key=gold.fact_key or "",
        predicted_fact_key=prediction.attribute_name,
        value_status=comparison.status.value,
        value_reason=comparison.reason,
        value_type_matched=comparison.value_type_matched,
        structured_value_matched=comparison.structured_value_matched,
        attribute_value_matched=attribute_value_matched,
        value_matched=value_matched,
        evidence_matched=evidence_matched,
        fact_correct=fact_correct,
        evidence_quote_count=evidence.quote_count,
        locatable_evidence_quote_count=evidence.locatable_quote_count,
        covered_gold_quote_count=evidence.covered_gold_quote_count,
        semantic_judge_reason=semantic_reason,
    )


def _evidence_is_acceptable(
    gold: GoldCandidate,
    evidence: EvidenceEvaluation,
    source_text: str | None,
) -> bool:
    if gold.evidence_quotes and not evidence.has_gold_quote_coverage:
        return False
    if source_text is not None and not evidence.all_prediction_quotes_locatable:
        return False
    return True


def _empty_aggregate() -> dict[str, int]:
    return {
        "goldExtract": 0,
        "goldDoNotExtract": 0,
        "goldDoNotExtractScored": 0,
        "goldDoNotExtractUnscored": 0,
        "goldReviewExcluded": 0,
        "characterDiscoveryExcluded": 0,
        "predictionTotal": 0,
        "predictions": 0,
        "reviewExcludedPredictions": 0,
        "detectionMatches": 0,
        "factCorrect": 0,
        "semanticPending": 0,
        "valueTypeCorrect": 0,
        "structuredValueEvaluated": 0,
        "structuredValueCorrect": 0,
        "attributeValueCorrect": 0,
        "hardNegativeViolations": 0,
        "duplicates": 0,
        "weightedGold": 0,
        "weightedDetectionMatches": 0,
        "weightedFactCorrect": 0,
        "evidenceQuotes": 0,
        "locatableEvidenceQuotes": 0,
        "matchedWithPredictionEvidence": 0,
        "matchedWithGoldEvidence": 0,
        "matchedWithCoveredGoldEvidence": 0,
        "judgeInputTokens": 0,
        "judgeCachedInputTokens": 0,
        "judgeOutputTokens": 0,
        "unknownSubjectPredictions": 0,
        "subjectOnlyFailures": 0,
        "ambiguousUnknownSubjectPredictions": 0,
        "pendingUnknownSubjectPredictions": 0,
        "unmatchedUnknownSubjectPredictions": 0,
    }


def _merge_aggregate(aggregate: dict[str, int], counts: dict[str, int]) -> None:
    for key, value in counts.items():
        aggregate[key] += value


def _build_metrics(counts: dict[str, int]) -> dict[str, float | bool | None]:
    detection_precision = _ratio(counts["detectionMatches"], counts["predictions"])
    detection_recall = _ratio(counts["detectionMatches"], counts["goldExtract"])
    semantic_complete = counts["semanticPending"] == 0
    # 의미 판정이 남았는데 이를 오답으로 간주하면 지표가 실행 옵션에 따라 왜곡된다.
    fact_precision = (
        _ratio(counts["factCorrect"], counts["predictions"]) if semantic_complete else None
    )
    fact_recall = (
        _ratio(counts["factCorrect"], counts["goldExtract"]) if semantic_complete else None
    )
    return {
        "detectionPrecision": detection_precision,
        "detectionRecall": detection_recall,
        "detectionF1": _f1(detection_precision, detection_recall),
        "factMetricsComplete": semantic_complete,
        "factPrecision": fact_precision,
        "factRecall": fact_recall,
        "factF1": _f1(fact_precision, fact_recall),
        "weightedDetectionRecall": _ratio(
            counts["weightedDetectionMatches"],
            counts["weightedGold"],
        ),
        "weightedFactRecall": (
            _ratio(counts["weightedFactCorrect"], counts["weightedGold"])
            if semantic_complete
            else None
        ),
        "valueTypeAccuracy": _ratio(
            counts["valueTypeCorrect"],
            counts["detectionMatches"],
        ),
        "attributeValueAccuracy": (
            _ratio(counts["attributeValueCorrect"], counts["detectionMatches"])
            if semantic_complete
            else None
        ),
        "structuredValueAccuracy": _ratio(
            counts["structuredValueCorrect"],
            counts["structuredValueEvaluated"],
        ),
        "evidenceProvidedRate": _ratio(
            counts["matchedWithPredictionEvidence"],
            counts["detectionMatches"],
        ),
        "evidenceLocatableRate": _ratio(
            counts["locatableEvidenceQuotes"],
            counts["evidenceQuotes"],
        ),
        "goldEvidenceCoverageRate": _ratio(
            counts["matchedWithCoveredGoldEvidence"],
            counts["matchedWithGoldEvidence"],
        ),
        "hardNegativeViolationRate": _ratio(
            counts["hardNegativeViolations"],
            counts["goldDoNotExtractScored"],
        ),
        "duplicatePredictionRate": _ratio(counts["duplicates"], counts["predictions"]),
        "unknownSubjectPredictionRate": _ratio(
            counts["unknownSubjectPredictions"],
            counts["predictionTotal"],
        ),
        "subjectOnlyFailureRate": _ratio(
            counts["subjectOnlyFailures"],
            counts["goldExtract"],
        ),
        "unknownSubjectRecoverableRate": _ratio(
            counts["subjectOnlyFailures"],
            counts["unknownSubjectPredictions"],
        ),
        "ambiguousUnknownSubjectRate": _ratio(
            counts["ambiguousUnknownSubjectPredictions"],
            counts["unknownSubjectPredictions"],
        ),
        "pendingUnknownSubjectRate": _ratio(
            counts["pendingUnknownSubjectPredictions"],
            counts["unknownSubjectPredictions"],
        ),
    }


def _importance_weight(gold: GoldCandidate) -> int:
    if gold.importance is None:
        raise ValueError(f"EXTRACT gold row has no importance: {gold.gold_id}")
    return gold.importance.weight


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 6) if denominator else None


def _f1(precision: float | None, recall: float | None) -> float | None:
    if precision is None or recall is None:
        return None
    if precision + recall == 0:
        return 0.0
    return round(2 * precision * recall / (precision + recall), 6)
