from __future__ import annotations

import unicodedata
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass
from time import perf_counter_ns
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

from app.analysis.world_setting_comparator import (
    WorldSettingComparator,
    WorldSettingSubjectResolver,
)
from app.analysis.world_setting_schemas import WorldSettingComparisonDecision
from app.domain.enums import (
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
    WorldSettingSubjectResolutionType,
)
from app.llm.protocols import TextGenerationClient
from app.schemas.worker import WorkerWorldSettingComparisonBatchCandidate
from app.usage.metering import (
    AiTokenLedgerApi,
    MeteredTextGenerationClient,
)
from evals.world_setting_comparison.replay_report import (
    ReplayArmOutcome,
    ReplayProposal,
    build_replay_report,
)
from evals.world_setting_comparison.replay_snapshot import (
    ReplayCandidate,
    ReplayDataset,
    ReplayEpisode,
    backend_duplicate_key,
)

MAX_BATCH_CANDIDATES = 20
MAX_BATCH_INPUT_CHARACTERS = 30_000
MAX_SUBJECT_RESOLUTION_TARGETS = 20


class NoOpAiTokenLedger(AiTokenLedgerApi):
    """Replay-only ledger: exercise metering without reserving production quota."""

    async def reserve_ai_tokens(
        self,
        request_id: UUID,
        analysis_job_id: UUID,
        purpose: str,
        attempt: int,
        model_name: str,
        reserved_tokens: int,
        lease_token: UUID,
    ) -> None:
        return None

    async def settle_ai_tokens(
        self,
        request_id: UUID,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        outcome: str,
    ) -> None:
        return None

    async def release_ai_tokens(self, request_id: UUID, outcome: str) -> None:
        return None


@dataclass(frozen=True)
class _SubjectResolution:
    candidate: ReplayCandidate
    resolution_type: WorldSettingSubjectResolutionType
    canonical_subject_key: str
    canonical_subject_name: str
    target_world_setting_ids: tuple[UUID, ...]


class WorldSettingComparisonReplayRunner:
    def __init__(
        self,
        delegate: TextGenerationClient,
        model: str,
        monotonic_ns: Callable[[], int] = perf_counter_ns,
    ) -> None:
        if not model.strip():
            raise ValueError("Replay model must not be blank.")
        self.delegate = delegate
        self.model = model.strip()
        self.monotonic_ns = monotonic_ns

    async def run(self, dataset: ReplayDataset) -> dict[str, Any]:
        single = await self._run_single_arm(dataset)
        batch = await self._run_batch_arm(dataset)
        return build_replay_report(dataset, self.model, single, batch)

    async def _run_single_arm(self, dataset: ReplayDataset) -> ReplayArmOutcome:
        client = self._metered_client(dataset, "single")
        resolver, comparator = self._components(client)
        proposals: list[ReplayProposal] = []
        for episode in dataset.episodes:
            resolutions = await _resolve_episode_subjects(episode, resolver)
            targets_by_id = episode.targets_by_id()
            for resolution in resolutions:
                targets = [
                    targets_by_id[target_id].as_worker_target()
                    for target_id in resolution.target_world_setting_ids
                ]
                decision, _ = await comparator.compare(
                    resolution.candidate.payload,
                    targets,
                )
                proposals.append(
                    _proposal_from_decision(
                        episode_no=episode.episode_no,
                        category=str(resolution.candidate.payload.category),
                        source_candidate_ids=(resolution.candidate.payload.candidate_id,),
                        resolution=resolution,
                        decision=decision,
                        targets=targets,
                    )
                )
        return ReplayArmOutcome(
            batch_count=dataset.candidate_count,
            proposals=tuple(proposals),
            usage=client.usage_snapshot(),
        )

    async def _run_batch_arm(self, dataset: ReplayDataset) -> ReplayArmOutcome:
        client = self._metered_client(dataset, "batch")
        resolver, comparator = self._components(client)
        proposals: list[ReplayProposal] = []
        batch_count = 0
        for episode in dataset.episodes:
            resolutions = await _resolve_episode_subjects(episode, resolver)
            targets_by_id = episode.targets_by_id()
            for grouped_resolutions in _batch_groups(resolutions):
                batch_count += 1
                if _exceeds_batch_limit(grouped_resolutions):
                    proposals.extend(
                        _overflow_proposal(episode.episode_no, resolution)
                        for resolution in grouped_resolutions
                    )
                    continue
                canonical_resolution = grouped_resolutions[0]
                target_ids = canonical_resolution.target_world_setting_ids
                if any(
                    resolution.target_world_setting_ids != target_ids
                    for resolution in grouped_resolutions
                ):
                    raise ValueError(
                        "A canonical replay batch resolved to inconsistent target sets."
                    )
                targets = [
                    targets_by_id[target_id].as_worker_target()
                    for target_id in target_ids
                ]
                candidates = [
                    _as_batch_candidate(index, resolution.candidate)
                    for index, resolution in enumerate(grouped_resolutions, start=1)
                ]
                result, _ = await comparator.compare_batch(
                    category=str(canonical_resolution.candidate.payload.category),
                    candidates=candidates,
                    targets=targets,
                )
                resolutions_by_ref = {
                    candidate.candidate_ref: resolution
                    for candidate, resolution in zip(
                        candidates,
                        grouped_resolutions,
                        strict=True,
                    )
                }
                for decision in result.decisions:
                    decision_resolutions = [
                        resolutions_by_ref[source_ref]
                        for source_ref in decision.source_candidate_refs
                    ]
                    proposals.append(
                        _proposal_from_decision(
                            episode_no=episode.episode_no,
                            category=str(
                                canonical_resolution.candidate.payload.category
                            ),
                            source_candidate_ids=tuple(
                                resolution.candidate.payload.candidate_id
                                for resolution in decision_resolutions
                            ),
                            resolution=canonical_resolution,
                            decision=decision,
                            targets=targets,
                        )
                    )
        return ReplayArmOutcome(
            batch_count=batch_count,
            proposals=tuple(proposals),
            usage=client.usage_snapshot(),
        )

    def _metered_client(
        self,
        dataset: ReplayDataset,
        arm: str,
    ) -> MeteredTextGenerationClient:
        return MeteredTextGenerationClient(
            delegate=self.delegate,
            ledger=NoOpAiTokenLedger(),
            analysis_job_id=uuid5(
                NAMESPACE_URL,
                f"catchhole-world-replay:{dataset.work_id}:{arm}:job",
            ),
            purpose="WORLD_SETTING_COMPARISON_REPLAY",
            default_model=self.model,
            lease_token=uuid5(
                NAMESPACE_URL,
                f"catchhole-world-replay:{dataset.work_id}:{arm}:lease",
            ),
            monotonic_ns=self.monotonic_ns,
        )

    def _components(
        self,
        client: MeteredTextGenerationClient,
    ) -> tuple[WorldSettingSubjectResolver, WorldSettingComparator]:
        return (
            WorldSettingSubjectResolver(llm_client=client, model=self.model),
            WorldSettingComparator(llm_client=client, model=self.model),
        )


async def _resolve_episode_subjects(
    episode: ReplayEpisode,
    resolver: WorldSettingSubjectResolver,
) -> tuple[_SubjectResolution, ...]:
    resolved: list[_SubjectResolution] = []
    for candidate in episode.candidates:
        subjects = episode.subjects(str(candidate.payload.category))
        candidate_key = _subject_match_key(candidate.payload.subject_name)
        exact_matches = [
            subject
            for subject in subjects
            if _subject_match_key(subject.subject_name) == candidate_key
        ]
        if exact_matches:
            if len(exact_matches) > MAX_SUBJECT_RESOLUTION_TARGETS:
                raise ValueError(
                    "Replay subject resolution found more than 20 "
                    "normalized exact targets."
                )
            target_ids = tuple(subject.world_setting_id for subject in exact_matches)
        else:
            target_ids = tuple(
                reference.world_setting_id
                for reference in await resolver.select_subjects(
                    candidate.payload,
                    subjects,
                )
            )
        target_ids = tuple(sorted(target_ids, key=_java_uuid_sort_key))
        targets_by_id = episode.targets_by_id()
        if any(target_id not in targets_by_id for target_id in target_ids):
            raise ValueError("Subject resolution selected a target outside the replay snapshot.")
        if not target_ids:
            resolution_type = WorldSettingSubjectResolutionType.NEW
            canonical_key = (
                f"NEW:{backend_duplicate_key(candidate.payload.subject_name)}"
            )
            canonical_name = candidate.payload.subject_name
        elif len(target_ids) == 1:
            resolution_type = WorldSettingSubjectResolutionType.EXISTING
            canonical_key = f"TARGET:{target_ids[0]}"
            canonical_name = targets_by_id[target_ids[0]].subject_name
        else:
            resolution_type = WorldSettingSubjectResolutionType.AMBIGUOUS
            canonical_key = f"AMBIGUOUS:{candidate.payload.candidate_id}"
            canonical_name = candidate.payload.subject_name
        resolved.append(
            _SubjectResolution(
                candidate=candidate,
                resolution_type=resolution_type,
                canonical_subject_key=canonical_key,
                canonical_subject_name=canonical_name,
                target_world_setting_ids=target_ids,
            )
        )
    return tuple(resolved)


def _batch_groups(
    resolutions: tuple[_SubjectResolution, ...],
) -> tuple[tuple[_SubjectResolution, ...], ...]:
    grouped: dict[tuple[str, str, str | None], list[_SubjectResolution]] = defaultdict(list)
    order: list[tuple[str, str, str | None]] = []
    for resolution in resolutions:
        payload = resolution.candidate.payload
        scope_key = (
            None
            if payload.scope_name is None
            else backend_duplicate_key(payload.scope_name)
        )
        key = (
            str(payload.category),
            resolution.canonical_subject_key,
            scope_key,
        )
        if key not in grouped:
            order.append(key)
        grouped[key].append(resolution)
    return tuple(
        tuple(sorted(grouped[key], key=_backend_batch_candidate_order))
        for key in order
    )


def _backend_batch_candidate_order(
    resolution: _SubjectResolution,
) -> tuple[int, Any, tuple[int, int]]:
    offsets = [
        evidence.start_offset
        for evidence in resolution.candidate.payload.evidence_spans
        if evidence.start_offset is not None
    ]
    return (
        min(offsets) if offsets else 2**31 - 1,
        resolution.candidate.created_at,
        _java_uuid_sort_key(resolution.candidate.payload.candidate_id),
    )


def _java_uuid_sort_key(value: UUID) -> tuple[int, int]:
    most_significant = value.int >> 64
    least_significant = value.int & ((1 << 64) - 1)
    return (
        most_significant - (1 << 64)
        if most_significant >= (1 << 63)
        else most_significant,
        least_significant - (1 << 64)
        if least_significant >= (1 << 63)
        else least_significant,
    )


def _exceeds_batch_limit(
    resolutions: tuple[_SubjectResolution, ...],
) -> bool:
    if len(resolutions) > MAX_BATCH_CANDIDATES:
        return True
    estimated_characters = 0
    for resolution in resolutions:
        payload = resolution.candidate.payload
        estimated_characters += _java_character_count(payload.subject_name)
        estimated_characters += _java_character_count(payload.scope_name)
        estimated_characters += _java_character_count(payload.setting_name)
        estimated_characters += _java_character_count(payload.extracted_value)
        estimated_characters += sum(
            _java_character_count(evidence.quote) for evidence in payload.evidence_spans
        )
        if estimated_characters > MAX_BATCH_INPUT_CHARACTERS:
            return True
    return False


def _java_character_count(value: str | None) -> int:
    if value is None:
        return 0
    return len(value.encode("utf-16-le")) // 2


def _as_batch_candidate(
    index: int,
    candidate: ReplayCandidate,
) -> WorkerWorldSettingComparisonBatchCandidate:
    payload = candidate.payload
    return WorkerWorldSettingComparisonBatchCandidate(
        candidate_ref=f"C{index}",
        candidate_id=payload.candidate_id,
        subject_name=payload.subject_name,
        scope_name=payload.scope_name,
        setting_name=payload.setting_name,
        extracted_value=payload.extracted_value,
        evidence_spans=payload.evidence_spans,
        extraction_confidence=payload.extraction_confidence,
    )


def _proposal_from_decision(
    *,
    episode_no: int,
    category: str,
    source_candidate_ids: tuple[UUID, ...],
    resolution: _SubjectResolution,
    decision: WorldSettingComparisonDecision,
    targets: list[Any],
) -> ReplayProposal:
    target_id_by_ref = {
        f"T{index}": target.world_setting_id
        for index, target in enumerate(targets, start=1)
    }
    target_id = (
        target_id_by_ref.get(decision.target_ref)
        if decision.target_ref is not None
        else None
    )
    if decision.target_ref is not None and target_id is None:
        raise ValueError("A comparison decision selected an unknown replay target.")
    return ReplayProposal(
        episode_no=episode_no,
        category=category,
        source_candidate_ids=source_candidate_ids,
        canonical_subject_key=resolution.canonical_subject_key,
        canonical_subject_name=resolution.canonical_subject_name,
        target_world_setting_id=target_id,
        matched_scope_name=decision.matched_scope_name,
        matched_setting_name=decision.matched_property_name,
        consolidation_status=str(decision.consolidation_status),
        operation=str(decision.operation),
        proposed_scope_name=decision.proposed_scope_name,
        proposed_setting_name=decision.proposed_setting_name,
        proposed_value=decision.proposed_value,
        existing_root_property_names_to_move=tuple(
            getattr(decision, "existing_root_property_names_to_move", ())
        ),
    )


def _overflow_proposal(
    episode_no: int,
    resolution: _SubjectResolution,
) -> ReplayProposal:
    payload = resolution.candidate.payload
    source_values = [
        value.strip()
        for value in payload.extracted_value.splitlines()
        if value.strip()
    ]
    return ReplayProposal(
        episode_no=episode_no,
        category=str(payload.category),
        source_candidate_ids=(payload.candidate_id,),
        canonical_subject_key=resolution.canonical_subject_key,
        canonical_subject_name=resolution.canonical_subject_name,
        target_world_setting_id=(
            resolution.target_world_setting_ids[0]
            if len(resolution.target_world_setting_ids) == 1
            else None
        ),
        matched_scope_name=None,
        matched_setting_name=None,
        consolidation_status=(
            WorldSettingConsolidationStatus.SINGLE.value
            if len(source_values) <= 1
            else WorldSettingConsolidationStatus.CONFLICT.value
        ),
        operation=WorldSettingOperation.REVIEW_REQUIRED.value,
        proposed_scope_name=payload.scope_name,
        proposed_setting_name=payload.setting_name,
        proposed_value=payload.extracted_value,
    )


def _subject_match_key(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip().casefold()
