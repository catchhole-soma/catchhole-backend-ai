import unicodedata

from app.analysis.evidence_span_resolver import resolve_evidence_span_offsets
from app.analysis.world_setting_schemas import ExtractedWorldSettingCandidate
from app.models.episode_chunk import EpisodeChunk
from app.schemas.worker import (
    WorkerEvidenceSpan,
    WorkerWorldSettingCandidatePublishItem,
)


class WorldSettingCandidateMapper:
    @staticmethod
    def to_publish_item(
        candidate: ExtractedWorldSettingCandidate,
        source_chunk: EpisodeChunk,
    ) -> WorkerWorldSettingCandidatePublishItem:
        resolved_spans = [
            resolve_evidence_span_offsets(
                span,
                chunk_text=source_chunk.chunk_text,
                chunk_start_offset=source_chunk.start_offset,
            )
            for span in candidate.evidence_spans
        ]
        return WorkerWorldSettingCandidatePublishItem(
            category=candidate.category,
            subject_name=candidate.subject_name,
            scope_name=candidate.scope_name,
            setting_name=candidate.setting_name,
            extracted_value=candidate.extracted_value,
            evidence_spans=[
                WorkerEvidenceSpan(
                    quote=span.quote,
                    start_offset=span.start_offset,
                    end_offset=span.end_offset,
                )
                for span in resolved_spans
            ],
            extraction_confidence=candidate.confidence,
            raw_extraction_json=candidate.model_dump(mode="json"),
        )

    @staticmethod
    def consolidate_by_key(
        candidates: list[WorkerWorldSettingCandidatePublishItem],
    ) -> list[WorkerWorldSettingCandidatePublishItem]:
        candidates_by_key: dict[
            tuple[str, str, str | None, str], list[WorkerWorldSettingCandidatePublishItem]
        ] = {}
        for candidate in candidates:
            key = (
                candidate.category,
                _normalized_name(candidate.subject_name),
                _normalized_optional_name(candidate.scope_name),
                _normalized_name(candidate.setting_name),
            )
            candidates_by_key.setdefault(key, []).append(candidate)
        return [
            _consolidate_candidates(group)
            for group in candidates_by_key.values()
        ]


def _consolidate_candidates(
    candidates: list[WorkerWorldSettingCandidatePublishItem],
) -> WorkerWorldSettingCandidatePublishItem:
    first = candidates[0]
    if len(candidates) == 1:
        return first

    source_values = _unique_values(candidate.extracted_value for candidate in candidates)
    evidence_spans = []
    evidence_keys: set[tuple[str, int | None, int | None]] = set()
    for candidate in candidates:
        for evidence in candidate.evidence_spans:
            evidence_key = (evidence.quote, evidence.start_offset, evidence.end_offset)
            if evidence_key in evidence_keys:
                continue
            evidence_keys.add(evidence_key)
            evidence_spans.append(evidence)

    return first.model_copy(update={
        "extracted_value": "\n".join(source_values),
        "evidence_spans": evidence_spans,
        "extraction_confidence": max(candidate.extraction_confidence for candidate in candidates),
        "raw_extraction_json": {
            "consolidationKey": {
                "category": first.category,
                "subjectName": first.subject_name,
                "scopeName": first.scope_name,
                "settingName": first.setting_name,
            },
            "sourceValues": source_values,
            "sourceCandidates": [candidate.raw_extraction_json for candidate in candidates],
        },
    })


def _unique_values(values) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        normalized = _normalized_name(value)
        if normalized in seen:
            continue
        seen.add(normalized)
        unique.append(value.strip())
    return unique


def _normalized_name(value: str) -> str:
    return unicodedata.normalize("NFC", value.strip()).casefold()


def _normalized_optional_name(value: str | None) -> str | None:
    return None if value is None else _normalized_name(value)
