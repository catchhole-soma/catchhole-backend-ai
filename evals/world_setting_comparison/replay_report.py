import re
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from app.domain.enums import WorldSettingConsolidationStatus, WorldSettingOperation
from app.usage.metering import TextGenerationUsageSnapshot
from evals.world_setting_comparison.replay_snapshot import (
    EPISODE_FROM,
    EPISODE_TO,
    RECONSTRUCTION_METHOD,
    ReplayDataset,
    backend_display_name,
    backend_duplicate_key,
    privacy_safe_hash,
)

REPORT_SCHEMA_VERSION = "world-setting-comparison-ab-replay-v1"
UUID_PATTERN = re.compile(
    r"(?i)(?<![0-9a-f])[0-9a-f]{8}(?:-[0-9a-f]{4}){3}-[0-9a-f]{12}"
    r"(?![0-9a-f])"
)
OPERATIONS = tuple(operation.value for operation in WorldSettingOperation)
CONSOLIDATION_STATUSES = tuple(
    status.value for status in WorldSettingConsolidationStatus
)


@dataclass(frozen=True)
class ReplayProposal:
    episode_no: int
    category: str
    source_candidate_ids: tuple[UUID, ...]
    canonical_subject_key: str
    canonical_subject_name: str
    target_world_setting_id: UUID | None
    matched_scope_name: str | None
    matched_setting_name: str | None
    consolidation_status: str
    operation: str
    proposed_scope_name: str | None
    proposed_setting_name: str
    proposed_value: str
    existing_root_property_names_to_move: tuple[str, ...] = ()


@dataclass(frozen=True)
class ReplayArmOutcome:
    batch_count: int
    proposals: tuple[ReplayProposal, ...]
    usage: TextGenerationUsageSnapshot


def build_replay_report(
    dataset: ReplayDataset,
    model: str,
    single: ReplayArmOutcome,
    batch: ReplayArmOutcome,
) -> dict[str, Any]:
    initial_state = _simulated_episode_state(dataset, ())
    single_state = _simulated_episode_state(dataset, single.proposals)
    batch_state = _simulated_episode_state(dataset, batch.proposals)
    single_summary = _arm_summary(dataset, single, initial_state, single_state)
    batch_summary = _arm_summary(dataset, batch, initial_state, batch_state)
    single_paths = set(single_state)
    batch_paths = set(batch_state)
    common_paths = single_paths & batch_paths
    report = {
        "schemaVersion": REPORT_SCHEMA_VERSION,
        "run": {
            "model": model,
            "comparisonArms": ["single", "batch"],
            "armOrder": ["single", "batch"],
            "cacheInterpretation": (
                "Cached input is reported separately because the later arm may reuse "
                "provider prompt cache entries warmed by the earlier arm."
            ),
            "contextReconstruction": RECONSTRUCTION_METHOD,
            "databaseAccess": "READ_ONLY",
            "tokenLedger": "NO_OP",
        },
        "dataset": {
            "datasetHash": dataset.dataset_hash,
            "episodeRange": {"from": EPISODE_FROM, "to": EPISODE_TO},
            "episodeCount": len(dataset.episodes),
            "candidateCount": dataset.candidate_count,
            "contextReconstructionExact": (
                dataset.reconstruction_fallback_count == 0
            ),
            "contextReconstructionFallbackCount": (
                dataset.reconstruction_fallback_count
            ),
        },
        "arms": {
            "single": single_summary,
            "batch": batch_summary,
        },
        "delta": {
            "providerRequestCount": (
                batch.usage.provider_request_count
                - single.usage.provider_request_count
            ),
            "providerLatencyMs": (
                batch.usage.provider_latency_ms - single.usage.provider_latency_ms
            ),
            "inputTokenCount": (
                batch.usage.input_token_count - single.usage.input_token_count
            ),
            "cachedInputTokenCount": (
                batch.usage.cached_input_token_count
                - single.usage.cached_input_token_count
            ),
            "outputTokenCount": (
                batch.usage.output_token_count - single.usage.output_token_count
            ),
            "batchCount": batch.batch_count - single.batch_count,
            "decisionCount": len(batch.proposals) - len(single.proposals),
            "clusterCount": len(batch.proposals) - len(single.proposals),
            "proposalHashesEqual": (
                batch_summary["proposalHash"] == single_summary["proposalHash"]
            ),
            "finalStateHashesEqual": (
                batch_summary["finalStateHash"] == single_summary["finalStateHash"]
            ),
            "addedPathCount": len(batch_paths - single_paths),
            "removedPathCount": len(single_paths - batch_paths),
            "changedPathCount": sum(
                single_state[path] != batch_state[path] for path in common_paths
            ),
        },
    }
    assert_privacy_safe_report(report)
    return report


def _arm_summary(
    dataset: ReplayDataset,
    outcome: ReplayArmOutcome,
    initial_state: dict[tuple[int, str, str, str | None, str], str],
    final_state: dict[tuple[int, str, str, str | None, str], str],
) -> dict[str, Any]:
    status_counts = Counter(
        proposal.consolidation_status for proposal in outcome.proposals
    )
    operation_counts = Counter(proposal.operation for proposal in outcome.proposals)
    clustered_candidate_count = sum(
        len(proposal.source_candidate_ids)
        for proposal in outcome.proposals
        if len(proposal.source_candidate_ids) > 1
    )
    singleton_candidate_count = sum(
        len(proposal.source_candidate_ids) == 1 for proposal in outcome.proposals
    )
    initial_paths = set(initial_state)
    final_paths = set(final_state)
    common_paths = initial_paths & final_paths
    return {
        "providerRequestCount": outcome.usage.provider_request_count,
        "providerLatencyMs": outcome.usage.provider_latency_ms,
        "inputTokenCount": outcome.usage.input_token_count,
        "cachedInputTokenCount": outcome.usage.cached_input_token_count,
        "outputTokenCount": outcome.usage.output_token_count,
        "batchCount": outcome.batch_count,
        "decisionCount": len(outcome.proposals),
        "clusterCount": len(outcome.proposals),
        "clusteredCandidateCount": clustered_candidate_count,
        "singletonCandidateCount": singleton_candidate_count,
        "consolidationStatusCounts": {
            status: status_counts[status] for status in CONSOLIDATION_STATUSES
        },
        "operationCounts": {
            operation: operation_counts[operation] for operation in OPERATIONS
        },
        "stateChangeCounts": {
            "addedPathCount": len(final_paths - initial_paths),
            "removedPathCount": len(initial_paths - final_paths),
            "changedPathCount": sum(
                initial_state[path] != final_state[path] for path in common_paths
            ),
        },
        "proposalHash": privacy_safe_hash(
            dataset.work_id,
            [_proposal_hash_payload(proposal) for proposal in outcome.proposals],
            "proposals",
        ),
        "finalStateHash": privacy_safe_hash(
            dataset.work_id,
            _state_hash_payload(final_state),
            "final-state",
        ),
    }


def _proposal_hash_payload(proposal: ReplayProposal) -> dict[str, Any]:
    return {
        "episode": proposal.episode_no,
        "category": proposal.category,
        "sources": sorted(str(candidate_id) for candidate_id in proposal.source_candidate_ids),
        "canonicalKey": proposal.canonical_subject_key,
        "canonicalName": proposal.canonical_subject_name,
        "target": (
            str(proposal.target_world_setting_id)
            if proposal.target_world_setting_id is not None
            else None
        ),
        "matchedScope": proposal.matched_scope_name,
        "matchedSetting": proposal.matched_setting_name,
        "status": proposal.consolidation_status,
        "operation": proposal.operation,
        "proposedScope": proposal.proposed_scope_name,
        "proposedSetting": proposal.proposed_setting_name,
        "proposedValue": proposal.proposed_value,
        "existingRootPropertyNamesToMove": sorted(
            backend_duplicate_key(property_name)
            for property_name in proposal.existing_root_property_names_to_move
        ),
    }


def _simulated_episode_state(
    dataset: ReplayDataset,
    proposals: tuple[ReplayProposal, ...],
) -> dict[tuple[int, str, str, str | None, str], str]:
    proposals_by_episode: dict[int, list[ReplayProposal]] = defaultdict(list)
    for proposal in proposals:
        proposals_by_episode[proposal.episode_no].append(proposal)

    aggregate_state: dict[tuple[int, str, str, str | None, str], str] = {}
    for episode in dataset.episodes:
        state: dict[tuple[str, str, str | None, str], str] = {
            (
                target.category,
                f"TARGET:{target.world_setting_id}",
                (
                    None
                    if property.scope_name is None
                    else backend_duplicate_key(property.scope_name)
                ),
                backend_duplicate_key(property.setting_name),
            ): backend_display_name(property.value)
            for target in episode.targets
            for property in target.properties
        }
        for proposal in proposals_by_episode[episode.episode_no]:
            _apply_proposal(state, proposal)
        aggregate_state.update(
            {
                (
                    episode.episode_no,
                    category,
                    target_key,
                    scope_name,
                    setting_name,
                ): value
                for (
                    category,
                    target_key,
                    scope_name,
                    setting_name,
                ), value in state.items()
            }
        )
    return aggregate_state


def _apply_proposal(
    state: dict[tuple[str, str, str | None, str], str],
    proposal: ReplayProposal,
) -> None:
    if proposal.operation in {
        WorldSettingOperation.EXCLUDE.value,
        WorldSettingOperation.REVIEW_REQUIRED.value,
    } or proposal.consolidation_status == WorldSettingConsolidationStatus.CONFLICT.value:
        return
    target_key = (
        f"TARGET:{proposal.target_world_setting_id}"
        if proposal.target_world_setting_id is not None
        else proposal.canonical_subject_key
    )
    if proposal.proposed_scope_name is not None:
        proposed_scope_key = backend_duplicate_key(proposal.proposed_scope_name)
        for property_name in proposal.existing_root_property_names_to_move:
            root_path = (
                proposal.category,
                target_key,
                None,
                backend_duplicate_key(property_name),
            )
            existing_value = state.pop(root_path, None)
            if existing_value is not None:
                state[
                    (
                        proposal.category,
                        target_key,
                        proposed_scope_key,
                        backend_duplicate_key(property_name),
                    )
                ] = existing_value
    if (
        proposal.operation in {
            WorldSettingOperation.UPDATE.value,
            WorldSettingOperation.MERGE.value,
        }
        and proposal.matched_setting_name is not None
    ):
        state.pop(
            (
                proposal.category,
                target_key,
                (
                    None
                    if proposal.matched_scope_name is None
                    else backend_duplicate_key(proposal.matched_scope_name)
                ),
                backend_duplicate_key(proposal.matched_setting_name),
            ),
            None,
        )
    state[
        (
            proposal.category,
            target_key,
            (
                None
                if proposal.proposed_scope_name is None
                else backend_duplicate_key(proposal.proposed_scope_name)
            ),
            backend_duplicate_key(proposal.proposed_setting_name),
        )
    ] = backend_display_name(proposal.proposed_value)


def _state_hash_payload(
    state: dict[tuple[int, str, str, str | None, str], str],
) -> list[dict[str, Any]]:
    return [
        {
            "episode": episode_no,
            "category": category,
            "target": target_key,
            "scope": scope_name,
            "setting": setting_name,
            "value": value,
        }
        for (
            episode_no,
            category,
            target_key,
            scope_name,
            setting_name,
        ), value in sorted(
            state.items(),
            key=lambda item: (
                item[0][0],
                item[0][1],
                item[0][2],
                item[0][3] or "",
                item[0][4],
            ),
        )
    ]


def assert_privacy_safe_report(report: dict[str, Any]) -> None:
    """Reject accidental identifiers even if report construction changes later."""

    forbidden_keys = {
        "workId",
        "episodeId",
        "candidateId",
        "targetWorldSettingId",
        "subjectName",
        "scopeName",
        "settingName",
        "value",
        "evidence",
        "quote",
    }

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            leaked_keys = forbidden_keys & set(value)
            if leaked_keys:
                raise ValueError("Replay report contains a forbidden identifying field.")
            for nested in value.values():
                visit(nested)
            return
        if isinstance(value, list):
            for nested in value:
                visit(nested)
            return
        if isinstance(value, str) and UUID_PATTERN.search(value):
            raise ValueError("Replay report contains a raw UUID.")

    visit(report)
