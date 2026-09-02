from collections import Counter

from app.schemas.worker import (
    WorkerWorldSettingComparisonBatchCandidate,
    WorkerWorldSettingComparisonBatchCompleteRequest,
    WorkerWorldSettingComparisonBatchPayload,
)
from evals.world_setting_comparison.models import (
    RuntimeCanonicalDecision,
    RuntimeEvidenceSpan,
    RuntimeSourceCandidate,
    WorldSettingComparisonRuntimeResult,
)


def adapt_batch_completion_for_evaluation(
    batch: WorkerWorldSettingComparisonBatchPayload,
    completion: WorkerWorldSettingComparisonBatchCompleteRequest,
) -> WorldSettingComparisonRuntimeResult:
    """운영 complete DTO를 #139가 채점할 수 있는 정규화 결과로 변환한다."""

    candidates_by_ref = _index_candidates(batch.candidates)
    _validate_exact_source_coverage(candidates_by_ref, completion)

    return WorldSettingComparisonRuntimeResult(
        comparison_batch_id=batch.comparison_batch_id,
        work_id=batch.work_id,
        source_episode_id=batch.source_episode_id,
        category=batch.category,
        subject_resolution_type=batch.resolution_type,
        canonical_subject_key=batch.canonical_subject_key,
        canonical_subject_name=batch.canonical_subject_name,
        canonical_target_world_setting_ids=batch.resolved_target_world_setting_ids,
        decisions=[
            RuntimeCanonicalDecision(
                decision_ref=decision.decision_ref,
                source_candidates=[
                    _adapt_source_candidate(candidates_by_ref[source_ref])
                    for source_ref in decision.source_candidate_refs
                ],
                existing_root_property_names_to_move=(
                    decision.existing_root_property_names_to_move
                ),
                canonical_subject_name=decision.canonical_subject_name,
                target_world_setting_id=decision.target_world_setting_id,
                matched_scope_name=decision.matched_scope_name,
                matched_property_name=decision.matched_property_name,
                consolidation_status=decision.consolidation_status,
                operation=decision.suggested_operation,
                review_reason=decision.comparison_review_reason,
                proposed_scope_name=decision.proposed_scope_name,
                proposed_setting_name=decision.proposed_setting_name,
                proposed_value=decision.proposed_value,
                comparison_reason=decision.comparison_reason,
            )
            for decision in completion.decisions
        ],
    )


def _index_candidates(
    candidates: list[WorkerWorldSettingComparisonBatchCandidate],
) -> dict[str, WorkerWorldSettingComparisonBatchCandidate]:
    candidates_by_ref = {candidate.candidate_ref: candidate for candidate in candidates}
    if len(candidates_by_ref) != len(candidates):
        raise ValueError("Runtime batch contains duplicate candidate refs.")
    return candidates_by_ref


def _validate_exact_source_coverage(
    candidates_by_ref: dict[str, WorkerWorldSettingComparisonBatchCandidate],
    completion: WorkerWorldSettingComparisonBatchCompleteRequest,
) -> None:
    decision_refs = [decision.decision_ref for decision in completion.decisions]
    if len(set(decision_refs)) != len(decision_refs):
        raise ValueError("Runtime completion contains duplicate decision refs.")

    source_refs = [
        source_ref
        for decision in completion.decisions
        for source_ref in decision.source_candidate_refs
    ]
    source_counts = Counter(source_refs)
    unknown_refs = sorted(set(source_refs) - candidates_by_ref.keys())
    if unknown_refs:
        raise ValueError(f"Runtime completion contains unknown candidate refs: {unknown_refs}")

    duplicate_refs = sorted(ref for ref, count in source_counts.items() if count > 1)
    if duplicate_refs:
        raise ValueError(f"Runtime completion reuses candidate refs: {duplicate_refs}")

    missing_refs = sorted(candidates_by_ref.keys() - set(source_refs))
    if missing_refs:
        raise ValueError(f"Runtime completion omits candidate refs: {missing_refs}")


def _adapt_source_candidate(
    candidate: WorkerWorldSettingComparisonBatchCandidate,
) -> RuntimeSourceCandidate:
    return RuntimeSourceCandidate(
        candidate_id=candidate.candidate_id,
        raw_subject_name=candidate.subject_name,
        raw_scope_name=candidate.scope_name,
        raw_setting_name=candidate.setting_name,
        extracted_value=candidate.extracted_value,
        evidence_spans=[
            RuntimeEvidenceSpan(
                quote=evidence.quote,
                start_offset=evidence.start_offset,
                end_offset=evidence.end_offset,
            )
            for evidence in candidate.evidence_spans
        ],
    )
