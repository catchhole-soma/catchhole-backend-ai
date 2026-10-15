import json
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analysis.character_name_resolver import (
    CharacterNameMatch,
    KnownCharacter,
    located_discovery_key,
    normalize_character_name,
    normalize_known_characters,
    resolve_candidate_character,
)
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_character_subjects import OrderedCandidateBinding
from app.analysis.schemas import ExtractedSettingCandidate
from app.domain.enums import (
    AnalysisSourceKind,
    CharacterFactComparisonStatus,
    SettingCandidateKind,
    SettingCandidateMatchStatus,
)
from app.mappers.setting_candidate_mapper import SettingCandidateMapper
from app.models.setting_book_chunk import SettingBookChunk
from app.models.setting_candidate import SettingCandidate
from app.repositories.setting_candidate_repository import SettingCandidateRepository
from app.services.ordered_analysis_fence import (
    OrderedCandidateWriteContext,
    fence_ordered_candidate_write,
)


@dataclass(frozen=True)
class SettingCandidateSaveItem:
    episode_id: UUID | None
    source_content_s3_key: str | None
    candidate: ExtractedSettingCandidate
    ordered_binding: OrderedCandidateBinding | None = None
    source_content_version: str | None = None
    source_kind: AnalysisSourceKind = AnalysisSourceKind.EPISODE
    setting_book_id: UUID | None = None
    setting_book_revision_id: UUID | None = None


@dataclass(frozen=True)
class PreparedSettingCandidate:
    """DB 저장 직전 운영 handoff 후보와 이름 매칭 결과."""

    source_index: int
    candidate: ExtractedSettingCandidate
    character_match: CharacterNameMatch


class SettingCandidateService:
    def __init__(
        self,
        session_factory: Callable[[], Session], # 아무 인자도 안 받고 Session을 반환
        # Session을 인자로 Repository를 반환 암시
        repository_factory: Callable[[Session], SettingCandidateRepository] = SettingCandidateRepository,
    ) -> None:
        self.session_factory = session_factory
        self.repository_factory = repository_factory

    def replace_candidates_for_analysis_job(
        self,
        work_id: UUID,
        analysis_job_id: UUID,
        save_items: list[SettingCandidateSaveItem],
        known_characters: list[KnownCharacter],
        ordered_write_context: OrderedCandidateWriteContext | None = None,
    ) -> list[SettingCandidate]:
        is_book = (ordered_write_context is not None
                   and ordered_write_context.source_kind == AnalysisSourceKind.SETTING_BOOK)
        for item in save_items:
            if item.source_kind == AnalysisSourceKind.SETTING_BOOK:
                if (not is_book or item.episode_id is not None
                        or item.setting_book_id != ordered_write_context.setting_book_id
                        or item.setting_book_revision_id != ordered_write_context.setting_book_revision_id
                        or item.source_content_s3_key != ordered_write_context.content_s3_key):
                    raise ComparisonValidationError("Setting-book candidate source does not match its job.")
            elif is_book or item.setting_book_id is not None or item.setting_book_revision_id is not None:
                raise ComparisonValidationError("Episode candidate cannot use a setting-book write fence.")
        if ordered_write_context is None:
            if any(item.ordered_binding is not None for item in save_items):
                raise ValueError("Ordered subject bindings require an ordered write fence.")
            prepared_candidates = prepare_setting_candidates(
                [item.candidate for item in save_items], known_characters,
                source_identities=[(item.episode_id, item.source_content_version or item.source_content_s3_key)
                                   for item in save_items],
            )
        else:
            if any(item.ordered_binding is None for item in save_items):
                raise ValueError("Ordered candidates require explicit subject resolution.")
            prepared_candidates = _prepare_ordered_setting_candidates(save_items)
        candidates: list[SettingCandidate] = []
        for prepared in prepared_candidates:
            item = save_items[prepared.source_index]
            mapped_candidate = SettingCandidateMapper.to_entity(
                work_id=work_id,
                episode_id=item.episode_id,
                source_content_s3_key=item.source_content_s3_key,
                analysis_job_id=analysis_job_id,
                candidate=item.candidate,
                character_match=prepared.character_match,
                source_kind=item.source_kind,
                setting_book_id=item.setting_book_id,
                setting_book_revision_id=item.setting_book_revision_id,
            )
            mapped_candidate.raw_ai_result_json.pop('setting_book_identity_proof', None)
            if item.ordered_binding is not None:
                binding = item.ordered_binding
                if binding.book_identity_proof is not None:
                    proof = binding.book_identity_proof
                    source = proof.context.source
                    target_ref = (f'character:{binding.actual_character_id}'
                                  if binding.actual_character_id else binding.provisional_subject_key)
                    if (not is_book or proof.context.analysis_job_id != analysis_job_id
                            or source.setting_book_id != ordered_write_context.setting_book_id
                            or source.revision_id != ordered_write_context.setting_book_revision_id
                            or source.content_hash != ordered_write_context.context.source_hash
                            or source.reference_time_kind != ordered_write_context.reference_time_kind
                            or source.reference_episode_no != ordered_write_context.reference_episode_no
                            or proof.parent_target_ref != target_ref
                            or binding.match_status != SettingCandidateMatchStatus.MATCHED
                            or binding.subject_failure_code is not None):
                        raise ComparisonValidationError('Identity proof is outside its frozen source or binding.')
                    mapped_candidate.raw_ai_result_json['setting_book_identity_proof'] = proof.wire()
                if binding.actual_character_id is not None and binding.provisional_subject_key:
                    raise ValueError("Actual and provisional character identities are exclusive.")
                mapped_candidate.id = binding.candidate_id
                mapped_candidate.provisional_subject_key = binding.provisional_subject_key
                if (binding.provisional_subject_key is not None
                        and item.candidate.candidate_kind == SettingCandidateKind.SETTING):
                    mapped_candidate.comparison_status = CharacterFactComparisonStatus.PENDING
                if binding.subject_failure_code is not None:
                    if (binding.actual_character_id is not None or binding.provisional_subject_key
                            or binding.match_status != SettingCandidateMatchStatus.AMBIGUOUS
                            or item.candidate.candidate_kind != SettingCandidateKind.SETTING):
                        raise ValueError("Failed subject resolution cannot establish a character identity.")
                    mapped_candidate.comparison_status = CharacterFactComparisonStatus.FAILED
                    mapped_candidate.comparison_failure_code = binding.subject_failure_code
                    mapped_candidate.preparation_failure_stage = "SUBJECT_RESOLUTION"
                    mapped_candidate.comparison_error_message = "설정을 어느 인물에 연결할지 확인하지 못했습니다."
                mapped_candidate.raw_ai_result_json["orderedSubjectResolution"] = {
                    "provisionalSubjectKey": binding.provisional_subject_key,
                    "actualCharacterId": (str(binding.actual_character_id)
                                          if binding.actual_character_id else None),
                    "matchStatus": binding.match_status,
                    **({"failureCode": binding.subject_failure_code, "failureStage": "SUBJECT_RESOLUTION"}
                       if binding.subject_failure_code is not None else {}),
                }
            candidates.append(mapped_candidate)

        with self.session_factory() as session:
            repository = self.repository_factory(session)
            try:
                if ordered_write_context is not None:
                    fence_ordered_candidate_write(
                        session,
                        analysis_job_id=analysis_job_id,
                        work_id=work_id,
                        write_context=ordered_write_context,
                    )
                if is_book:
                    chunk_ids = {item.candidate.source_chunk_id for item in save_items}
                    owned = set(session.scalars(select(SettingBookChunk.id).where(
                        SettingBookChunk.id.in_(chunk_ids),
                        SettingBookChunk.analysis_job_id == analysis_job_id,
                        SettingBookChunk.setting_book_revision_id == ordered_write_context.setting_book_revision_id,
                    ))) if chunk_ids else set()
                    if owned != chunk_ids:
                        raise ComparisonValidationError("Setting-book candidate chunk is outside its source job.")
                # 같은 analysis_job_id 기준으로 재실행해도 후보가 중복 저장되지 않게 교체한다.
                if is_book:
                    repository.delete_by_setting_book_job(analysis_job_id,
                        ordered_write_context.setting_book_id, ordered_write_context.setting_book_revision_id)
                else:
                    repository.delete_by_analysis_job_id(analysis_job_id)
                saved_candidates = repository.save_all(candidates)
                session.commit()
            except Exception:
                session.rollback()
                raise

        return saved_candidates


def _prepare_ordered_setting_candidates(
    save_items: list[SettingCandidateSaveItem],
) -> list[PreparedSettingCandidate]:
    """Deduplicate only resolved facts anchored to the identical source occurrence.

    Names alone and equal values at different points in the story are not enough.
    Unknown offsets and unresolved identities retain every candidate for review.
    """
    prepared: list[PreparedSettingCandidate] = []
    seen: dict[tuple, int] = {}
    for index, item in enumerate(save_items):
        binding = item.ordered_binding
        assert binding is not None
        candidate = item.candidate
        row = PreparedSettingCandidate(index, candidate, CharacterNameMatch(
            matched_character_id=binding.actual_character_id, match_status=binding.match_status,
        ))
        key = None
        if (candidate.candidate_kind == SettingCandidateKind.SETTING
                and binding.subject_failure_code is None
                and (binding.actual_character_id is not None or binding.provisional_subject_key)
                and binding.match_status != SettingCandidateMatchStatus.AMBIGUOUS
                and (item.episode_id is not None or item.setting_book_id is not None)
                and item.source_content_s3_key
                and candidate.value_json is not None
                and all(span.start_offset is not None and span.end_offset is not None
                        and span.end_offset > span.start_offset for span in candidate.evidence_spans)):
            key = (
                item.source_kind, item.episode_id, item.setting_book_id,
                item.setting_book_revision_id or item.source_content_version or item.source_content_s3_key,
                binding.actual_character_id, binding.provisional_subject_key,
                candidate.attribute_name, candidate.value_type,
                json.dumps(candidate.value_json, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
                tuple(sorted((span.start_offset, span.end_offset, span.quote)
                             for span in candidate.evidence_spans)),
            )
        if key is not None and key in seen:
            prior = seen[key]
            confidence = candidate.confidence if candidate.confidence is not None else -1
            old_confidence = prepared[prior].candidate.confidence
            if confidence > (old_confidence if old_confidence is not None else -1):
                prepared[prior] = row
            continue
        if key is not None:
            seen[key] = len(prepared)
        prepared.append(row)
    return prepared


def prepare_setting_candidates(
    candidates: list[ExtractedSettingCandidate],
    known_characters: list[KnownCharacter],
    source_identities: list[tuple[UUID | None, str | None]] | None = None,
) -> list[PreparedSettingCandidate]:
    """운영 저장 경계의 이름 매칭·발견 필터·중복 제거를 순수 함수로 실행한다."""

    normalized_known_characters = normalize_known_characters(known_characters)
    prepared: list[PreparedSettingCandidate] = []
    seen_discoveries: set[tuple] = set()
    if source_identities is not None and len(source_identities) != len(candidates):
        raise ValueError("source_identities must align with candidates.")
    setting_candidate_by_key: dict[tuple[object, ...], tuple[int, float]] = {}
    for source_index, candidate in enumerate(candidates):
        character_match = resolve_candidate_character(
            candidate,
            normalized_known_characters,
        )
        if candidate.candidate_kind == SettingCandidateKind.CHARACTER_DISCOVERY:
            if character_match.match_status != SettingCandidateMatchStatus.UNRESOLVED:
                continue
            discovery_key = located_discovery_key(candidate)
            if discovery_key is not None and source_identities is not None:
                discovery_key = (source_identities[source_index], discovery_key)
            if discovery_key is not None and discovery_key in seen_discoveries:
                continue
            if discovery_key is not None:
                seen_discoveries.add(discovery_key)

        item = PreparedSettingCandidate(
            source_index=source_index,
            candidate=candidate,
            character_match=character_match,
        )
        duplicate_key = _setting_duplicate_key(
            candidate,
            character_match,
            None if source_identities is None else source_identities[source_index],
        )
        if duplicate_key is not None:
            confidence_score = candidate.confidence if candidate.confidence is not None else -1.0
            existing = setting_candidate_by_key.get(duplicate_key)
            if existing is not None:
                existing_index, existing_confidence = existing
                if confidence_score > existing_confidence:
                    prepared[existing_index] = item
                    setting_candidate_by_key[duplicate_key] = (
                        existing_index,
                        confidence_score,
                    )
                continue
            setting_candidate_by_key[duplicate_key] = (
                len(prepared),
                confidence_score,
            )
        prepared.append(item)
    return prepared


def _setting_duplicate_key(
    candidate: ExtractedSettingCandidate,
    character_match: CharacterNameMatch,
    source_identity: tuple[UUID | None, str | None] | None,
) -> tuple[object, ...] | None:
    if (
        candidate.candidate_kind != SettingCandidateKind.SETTING
        or character_match.match_status == SettingCandidateMatchStatus.AMBIGUOUS
        or candidate.attribute_name is None
        or candidate.value_type is None
        or candidate.value_json is None
        or source_identity is None
        or source_identity[0] is None
        or not source_identity[1]
    ):
        return None

    evidence_fingerprint = _evidence_fingerprint(candidate)
    if evidence_fingerprint is None:
        return None

    if character_match.matched_character_id is not None:
        subject_key = f"id:{character_match.matched_character_id}"
    else:
        normalized_name = normalize_character_name(candidate.entity_name)
        if not normalized_name:
            return None
        subject_key = f"name:{normalized_name}"

    canonical_value_json = json.dumps(
        candidate.value_json,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (
        source_identity[0],
        source_identity[1],
        subject_key,
        candidate.attribute_name,
        candidate.value_type,
        canonical_value_json,
        evidence_fingerprint,
    )


def _evidence_fingerprint(
    candidate: ExtractedSettingCandidate,
) -> tuple[tuple[int, int, str], ...] | None:
    evidence: set[tuple[int, int, str]] = set()
    for span in candidate.evidence_spans:
        quote = " ".join(span.quote.split())
        if (
            span.start_offset is None
            or span.end_offset is None
            or span.end_offset <= span.start_offset
            or not quote
        ):
            return None
        evidence.add((span.start_offset, span.end_offset, quote))
    return tuple(sorted(evidence)) if evidence else None
