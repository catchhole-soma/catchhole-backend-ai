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
    def deduplicate(
        candidates: list[WorkerWorldSettingCandidatePublishItem],
    ) -> list[WorkerWorldSettingCandidatePublishItem]:
        unique_candidates: dict[
            tuple[str, str, str, str], WorkerWorldSettingCandidatePublishItem
        ] = {}
        for candidate in candidates:
            key = (
                candidate.category,
                candidate.subject_name,
                candidate.setting_name,
                candidate.extracted_value,
            )
            if key not in unique_candidates:
                unique_candidates[key] = candidate
        return list(unique_candidates.values())
