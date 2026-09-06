from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from evals.multi_stage_setting.contracts import (
    CandidateKind,
    CharacterStage1Gold,
    CharacterStage1Prediction,
    EvaluationDomain,
    Stage1Gold,
    Stage1Prediction,
    UpstreamOutcome,
    WorldStage1Gold,
    WorldStage1Prediction,
    world_path_key,
)
from app.mappers.world_setting_candidate_mapper import normalize_world_setting_name
from evals.setting_extraction.assignment import maximum_weight_assignment
from evals.setting_extraction.evidence import EvidenceEvaluation, evaluate_evidence_quotes
from evals.setting_extraction.normalization import normalize_fact_key, normalize_text
from evals.setting_extraction.value_comparator import (
    ValueComparison,
    ValueComparisonStatus,
    compare_typed_value,
)


class FieldMatchStatus(StrEnum):
    MATCH = "MATCH"
    MISMATCH = "MISMATCH"
    SEMANTIC_JUDGE_REQUIRED = "SEMANTIC_JUDGE_REQUIRED"
    NOT_APPLICABLE = "NOT_APPLICABLE"


@dataclass(frozen=True)
class Stage1Match:
    gold: CharacterStage1Gold | WorldStage1Gold
    source_gold_ids: tuple[str, ...]
    prediction: CharacterStage1Prediction | WorldStage1Prediction
    entity_or_subject_matched: bool
    path_or_fact_matched: bool
    value_status: FieldMatchStatus
    value_type_matched: bool | None
    structured_value_matched: bool | None
    evidence: EvidenceEvaluation
    upstream_outcome: UpstreamOutcome
    assignment_weight: int

    @property
    def identity_matched(self) -> bool:
        return self.entity_or_subject_matched and self.path_or_fact_matched


@dataclass(frozen=True)
class Stage1MatchingResult:
    matches: tuple[Stage1Match, ...]
    missed_gold: tuple[CharacterStage1Gold | WorldStage1Gold, ...]
    missed_source_gold_ids: tuple[tuple[str, ...], ...]
    extra_predictions: tuple[CharacterStage1Prediction | WorldStage1Prediction, ...]
    hard_negative_hits: tuple[tuple[str, str], ...]
    raw_prediction_count: int
    handoff_prediction_count: int
    grouped_prediction_count: int
    gold_group_count: int

    @property
    def prediction_id_by_gold_id(self) -> dict[str, str]:
        return {
            gold_id: match.prediction.candidate_id
            for match in self.matches
            for gold_id in match.source_gold_ids
        }

    @property
    def outcome_by_gold_id(self) -> dict[str, UpstreamOutcome]:
        result = {
            gold_id: match.upstream_outcome
            for match in self.matches
            for gold_id in match.source_gold_ids
        }
        result.update(
            {
                gold_id: UpstreamOutcome.UPSTREAM_MISSING
                for group_ids in self.missed_source_gold_ids
                for gold_id in group_ids
            }
        )
        return result


@dataclass(frozen=True)
class _Stage1GoldGroup:
    gold: CharacterStage1Gold | WorldStage1Gold
    source_gold_ids: tuple[str, ...]


def match_stage1(
    gold_rows: list[Stage1Gold],
    predictions: list[Stage1Prediction],
    *,
    domain: EvaluationDomain,
    source_text: str | None,
    raw_prediction_count: int | None = None,
) -> Stage1MatchingResult:
    domain_gold = [item for item in gold_rows if item.domain == domain]
    positives = [item for item in domain_gold if item.decision == "EXTRACT"]
    positive_groups = _group_positive_gold(positives, domain)
    hard_negatives = [item for item in domain_gold if item.decision == "DO_NOT_EXTRACT"]
    domain_predictions = [item for item in predictions if item.domain == domain]
    handoff_count = len(domain_predictions)
    grouped_predictions = (
        consolidate_world_predictions(domain_predictions)
        if domain == EvaluationDomain.WORLD
        else domain_predictions
    )

    weights = [
        [
            _assignment_weight(group.gold, prediction)
            for prediction in grouped_predictions
        ]
        for group in positive_groups
    ]
    assignments = maximum_weight_assignment(weights) if weights else []
    assigned_gold: set[int] = set()
    assigned_predictions: set[int] = set()
    matches: list[Stage1Match] = []
    for gold_index, prediction_index in assignments:
        group = positive_groups[gold_index]
        gold = group.gold
        prediction = grouped_predictions[prediction_index]
        match = _evaluate_pair(
            gold,
            prediction,
            source_text=source_text,
            weight=weights[gold_index][prediction_index],
            source_gold_ids=group.source_gold_ids,
        )
        # Hungarian은 약한 값/근거 우연도 양수일 수 있다. 최소 한 identity 축이 맞아야
        # candidate reach로 인정한다.
        if not (match.entity_or_subject_matched or match.path_or_fact_matched):
            continue
        assigned_gold.add(gold_index)
        assigned_predictions.add(prediction_index)
        matches.append(match)

    missed_groups = tuple(
        group
        for index, group in enumerate(positive_groups)
        if index not in assigned_gold
    )
    extras = tuple(
        prediction
        for index, prediction in enumerate(grouped_predictions)
        if index not in assigned_predictions
    )
    hard_negative_hits = tuple(
        (negative.gold_id, prediction.candidate_id)
        for negative in hard_negatives
        for prediction in grouped_predictions
        if _hard_negative_matches(negative, prediction)
    )
    return Stage1MatchingResult(
        matches=tuple(matches),
        missed_gold=tuple(group.gold for group in missed_groups),
        missed_source_gold_ids=tuple(
            group.source_gold_ids for group in missed_groups
        ),
        extra_predictions=extras,
        hard_negative_hits=hard_negative_hits,
        raw_prediction_count=(
            len(domain_predictions) if raw_prediction_count is None else raw_prediction_count
        ),
        handoff_prediction_count=handoff_count,
        grouped_prediction_count=len(grouped_predictions),
        gold_group_count=len(positive_groups),
    )


def _group_positive_gold(
    positives: list[Stage1Gold],
    domain: EvaluationDomain,
) -> list[_Stage1GoldGroup]:
    if domain != EvaluationDomain.WORLD:
        return [
            _Stage1GoldGroup(gold=item, source_gold_ids=(item.gold_id,))
            for item in positives
        ]
    groups: dict[tuple[str, str, str, str], list[WorldStage1Gold]] = {}
    for item in positives:
        if not isinstance(item, WorldStage1Gold):
            continue
        groups.setdefault(
            world_path_key(
                item.category,
                item.subject_name,
                item.scope_name,
                item.setting_name,
            ),
            [],
        ).append(item)

    result: list[_Stage1GoldGroup] = []
    for rows in groups.values():
        primary = rows[0]
        values: list[str] = []
        seen_values: set[str] = set()
        evidence: list[str] = []
        seen_evidence: set[str] = set()
        accepted_setting_name_aliases: list[str] = []
        seen_setting_name_aliases: set[str] = set()
        for row in rows:
            for value in row.source_values:
                normalized = normalize_world_setting_name(value)
                if normalized not in seen_values:
                    seen_values.add(normalized)
                    values.append(value)
            for quote in row.evidence_quotes:
                normalized = normalize_text(quote)
                if normalized not in seen_evidence:
                    seen_evidence.add(normalized)
                    evidence.append(quote)
            for alias in row.accepted_setting_name_aliases:
                normalized = normalize_world_setting_name(alias)
                if normalized not in seen_setting_name_aliases:
                    seen_setting_name_aliases.add(normalized)
                    accepted_setting_name_aliases.append(alias)
        strongest = max(
            rows,
            key=lambda row: row.importance.weight if row.importance is not None else 1,
        )
        grouped = primary.model_copy(
            update={
                "source_values": values,
                "evidence_quotes": evidence,
                "importance": strongest.importance,
                "accepted_setting_name_aliases": accepted_setting_name_aliases,
            }
        )
        result.append(
            _Stage1GoldGroup(
                gold=grouped,
                source_gold_ids=tuple(row.gold_id for row in rows),
            )
        )
    return result


def consolidate_world_predictions(
    predictions: list[Stage1Prediction],
) -> list[WorldStage1Prediction]:
    groups: dict[tuple[str, str, str, str], WorldStage1Prediction] = {}
    for prediction in predictions:
        if not isinstance(prediction, WorldStage1Prediction):
            continue
        key = world_path_key(
            prediction.category,
            prediction.subject_name,
            prediction.scope_name,
            prediction.setting_name,
        )
        current = groups.get(key)
        if current is None:
            groups[key] = prediction
            continue
        values = list(current.source_values)
        normalized_values = {
            normalize_world_setting_name(value) for value in values
        }
        for value in prediction.source_values:
            normalized = normalize_world_setting_name(value)
            if normalized not in normalized_values:
                normalized_values.add(normalized)
                values.append(value)
        evidence = list(current.evidence_spans)
        evidence_keys = {
            (item.quote, item.start_offset, item.end_offset) for item in evidence
        }
        for span in prediction.evidence_spans:
            key_span = (span.quote, span.start_offset, span.end_offset)
            if key_span not in evidence_keys:
                evidence_keys.add(key_span)
                evidence.append(span)
        groups[key] = current.model_copy(
            update={
                "source_values": values,
                "evidence_spans": evidence,
                "confidence": (
                    max(
                        value
                        for value in (current.confidence, prediction.confidence)
                        if value is not None
                    )
                    if current.confidence is not None or prediction.confidence is not None
                    else None
                ),
            }
        )
    return list(groups.values())


def _assignment_weight(
    gold: CharacterStage1Gold | WorldStage1Gold,
    prediction: CharacterStage1Prediction | WorldStage1Prediction,
) -> int:
    if gold.domain != prediction.domain or gold.candidate_kind != prediction.candidate_kind:
        return 0
    evidence_overlap = any(
        normalize_text(quote) in normalize_text(span.quote)
        or normalize_text(span.quote) in normalize_text(quote)
        for quote in gold.evidence_quotes
        for span in prediction.evidence_spans
        if quote and span.quote
    )
    if isinstance(gold, CharacterStage1Gold) and isinstance(
        prediction, CharacterStage1Prediction
    ):
        entity = _character_entity_matches(gold, prediction)
        if gold.candidate_kind == CandidateKind.CHARACTER_DISCOVERY:
            return 200 + 120 * entity + 30 * evidence_overlap
        fact = _character_fact_matches(gold, prediction)
        value = normalize_text(gold.display_value) == normalize_text(prediction.display_value)
        return 200 + 100 * fact + 80 * entity + 30 * value + 20 * evidence_overlap
    if isinstance(gold, WorldStage1Gold) and isinstance(prediction, WorldStage1Prediction):
        gold_path = world_path_key(
            gold.category,
            gold.subject_name,
            gold.scope_name,
            gold.setting_name,
        )
        prediction_path = world_path_key(
            prediction.category,
            prediction.subject_name,
            prediction.scope_name,
            prediction.setting_name,
        )
        category = gold_path[0] == prediction_path[0]
        subject = gold_path[1] == prediction_path[1]
        scope = gold_path[2] == prediction_path[2]
        setting = _world_setting_name_matches(gold, prediction.setting_name)
        value = any(
            normalize_text(expected) == normalize_text(actual)
            for expected in gold.source_values
            for actual in prediction.source_values
        )
        return (
            200
            + 50 * category
            + 90 * subject
            + 40 * scope
            + 100 * setting
            + 30 * value
            + 20 * evidence_overlap
        )
    return 0


def _evaluate_pair(
    gold: CharacterStage1Gold | WorldStage1Gold,
    prediction: CharacterStage1Prediction | WorldStage1Prediction,
    *,
    source_text: str | None,
    weight: int,
    source_gold_ids: tuple[str, ...],
) -> Stage1Match:
    evidence = evaluate_evidence_quotes(
        gold.evidence_quotes,
        [span.quote for span in prediction.evidence_spans],
        source_text,
    )
    if isinstance(gold, CharacterStage1Gold) and isinstance(
        prediction, CharacterStage1Prediction
    ):
        entity_matched = _character_entity_matches(gold, prediction)
        if gold.candidate_kind == CandidateKind.CHARACTER_DISCOVERY:
            return Stage1Match(
                gold=gold,
                source_gold_ids=source_gold_ids,
                prediction=prediction,
                entity_or_subject_matched=entity_matched,
                path_or_fact_matched=True,
                value_status=FieldMatchStatus.NOT_APPLICABLE,
                value_type_matched=None,
                structured_value_matched=None,
                evidence=evidence,
                upstream_outcome=(
                    UpstreamOutcome.REACHED
                    if entity_matched
                    else UpstreamOutcome.UPSTREAM_BLOCKED_SUBJECT
                ),
                assignment_weight=weight,
            )
        fact_matched = _character_fact_matches(gold, prediction)
        value = compare_typed_value(
            value_type=gold.value_type,
            expected_display_value=gold.display_value,
            actual_display_value=prediction.display_value,
            expected_value_json=gold.value_json if gold.structured_scorable else None,
            actual_value_json=prediction.value_json,
            actual_value_type=prediction.value_type,
        )
        value_status = _value_status(value)
        explicitly_blocked_subject = prediction.match_status in {
            "AMBIGUOUS",
            "UNRESOLVED",
            "WAITING_FOR_CHARACTER_MATCH",
        }
        if explicitly_blocked_subject or not entity_matched:
            upstream = UpstreamOutcome.UPSTREAM_BLOCKED_SUBJECT
        elif not fact_matched or value.status == ValueComparisonStatus.MISMATCH:
            upstream = UpstreamOutcome.UPSTREAM_VALUE_ERROR
        else:
            upstream = UpstreamOutcome.REACHED
        return Stage1Match(
            gold=gold,
            source_gold_ids=source_gold_ids,
            prediction=prediction,
            entity_or_subject_matched=entity_matched,
            path_or_fact_matched=fact_matched,
            value_status=value_status,
            value_type_matched=value.value_type_matched,
            structured_value_matched=value.structured_value_matched,
            evidence=evidence,
            upstream_outcome=upstream,
            assignment_weight=weight,
        )

    assert isinstance(gold, WorldStage1Gold)
    assert isinstance(prediction, WorldStage1Prediction)
    gold_path = world_path_key(
        gold.category,
        gold.subject_name,
        gold.scope_name,
        gold.setting_name,
    )
    prediction_path = world_path_key(
        prediction.category,
        prediction.subject_name,
        prediction.scope_name,
        prediction.setting_name,
    )
    subject_matched = gold_path[:2] == prediction_path[:2]
    path_matched = (
        gold_path[2] == prediction_path[2]
        and _world_setting_name_matches(gold, prediction.setting_name)
    )
    expected_values = {normalize_text(value) for value in gold.source_values}
    actual_values = {normalize_text(value) for value in prediction.source_values}
    if expected_values == actual_values:
        status = FieldMatchStatus.MATCH
        upstream = UpstreamOutcome.REACHED
    elif actual_values and actual_values < expected_values:
        status = FieldMatchStatus.MISMATCH
        upstream = UpstreamOutcome.UPSTREAM_PARTIAL
    elif expected_values.isdisjoint(actual_values):
        status = FieldMatchStatus.SEMANTIC_JUDGE_REQUIRED
        # 서술형 표현 차이는 semantic judge 전에는 upstream 오류로 단정하지 않는다.
        upstream = UpstreamOutcome.REACHED
    else:
        status = FieldMatchStatus.MISMATCH
        upstream = UpstreamOutcome.UPSTREAM_VALUE_ERROR
    if not subject_matched or not path_matched:
        upstream = UpstreamOutcome.UPSTREAM_VALUE_ERROR
    return Stage1Match(
        gold=gold,
        source_gold_ids=source_gold_ids,
        prediction=prediction,
        entity_or_subject_matched=subject_matched,
        path_or_fact_matched=path_matched,
        value_status=status,
        value_type_matched=None,
        structured_value_matched=None,
        evidence=evidence,
        upstream_outcome=upstream,
        assignment_weight=weight,
    )


def _character_entity_matches(
    gold: CharacterStage1Gold,
    prediction: CharacterStage1Prediction,
) -> bool:
    if (
        gold.candidate_kind != CandidateKind.CHARACTER_DISCOVERY
        and prediction.entity_ref is not None
    ):
        return normalize_text(gold.entity_ref) == normalize_text(prediction.entity_ref)
    prediction_name = prediction.matched_character_name or prediction.entity_name
    return normalize_text(gold.entity_name) == normalize_text(prediction_name)


def _character_fact_matches(
    gold: CharacterStage1Gold,
    prediction: CharacterStage1Prediction,
) -> bool:
    if prediction.fact_key is None:
        return False
    accepted = {normalize_fact_key(key) for key in gold.accepted_fact_keys}
    return normalize_fact_key(prediction.fact_key) in accepted and (
        gold.fact_type is None
        or prediction.fact_type is None
        or normalize_text(gold.fact_type) == normalize_text(prediction.fact_type)
    )


def _world_setting_name_matches(
    gold: WorldStage1Gold,
    prediction_setting_name: str,
) -> bool:
    accepted = {
        normalize_world_setting_name(name)
        for name in gold.accepted_setting_names
    }
    return normalize_world_setting_name(prediction_setting_name) in accepted


def _hard_negative_matches(
    gold: CharacterStage1Gold | WorldStage1Gold,
    prediction: CharacterStage1Prediction | WorldStage1Prediction,
) -> bool:
    if gold.domain != prediction.domain or gold.candidate_kind != prediction.candidate_kind:
        return False
    if isinstance(gold, CharacterStage1Gold) and isinstance(
        prediction, CharacterStage1Prediction
    ):
        return _character_entity_matches(gold, prediction) and (
            gold.candidate_kind == CandidateKind.CHARACTER_DISCOVERY
            or _character_fact_matches(gold, prediction)
        )
    if isinstance(gold, WorldStage1Gold) and isinstance(prediction, WorldStage1Prediction):
        gold_path = world_path_key(
            gold.category,
            gold.subject_name,
            gold.scope_name,
            gold.setting_name,
        )
        prediction_path = world_path_key(
            prediction.category,
            prediction.subject_name,
            prediction.scope_name,
            prediction.setting_name,
        )
        return (
            gold_path[:3] == prediction_path[:3]
            and _world_setting_name_matches(gold, prediction.setting_name)
        )
    return False


def _value_status(value: ValueComparison) -> FieldMatchStatus:
    return {
        ValueComparisonStatus.MATCH: FieldMatchStatus.MATCH,
        ValueComparisonStatus.MISMATCH: FieldMatchStatus.MISMATCH,
        ValueComparisonStatus.SEMANTIC_JUDGE_REQUIRED: (
            FieldMatchStatus.SEMANTIC_JUDGE_REQUIRED
        ),
    }[value.status]
