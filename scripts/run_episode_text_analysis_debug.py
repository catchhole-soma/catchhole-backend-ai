import argparse
from dataclasses import dataclass
import json
from pathlib import Path
from uuid import UUID, uuid4

from app.analysis.evidence_span_resolver import resolve_candidate_evidence_offsets
from app.analysis.schemas import ExtractedSettingCandidate
from app.analysis.setting_extractor import CharacterSettingExtractor
from app.chunking.chunk_splitter import EpisodeChunkDraft, split_into_chunks
from app.chunking.text_normalizer import normalize_text

# 로컬에서 텍스트 파일을 넣으면 청킹 -> llm로 설정후보 추출 -> 인용문 offset 보정 -> Setting_candidates 응답값을 json 파일로 주는 테스트 코드
# 실행 방법은 README.md 참고, 흐름 실험용 코드라 코드 디테일 부분은 안봐도 될 것 같아요!

@dataclass(frozen=True)
class DebugChunk:
    id: UUID
    draft: EpisodeChunkDraft


@dataclass(frozen=True)
class DebugCandidate:
    episode_id: UUID
    work_id: UUID
    analysis_job_id: UUID
    candidate: ExtractedSettingCandidate


def run_episode_text_analysis_debug(
    text_file: Path,
    episode_id: UUID,
    work_id: UUID,
    analysis_job_id: UUID,
    episode_no: int,
    episode_title: str | None,
    model_name: str | None,
    max_chunks: int | None,
    output_json: Path | None,
) -> dict:
    raw_text = text_file.read_text(encoding="utf-8")
    normalized_text = normalize_text(raw_text)
    drafts = split_into_chunks(normalized_text)
    chunks = [
        DebugChunk(
            id=uuid4(),
            draft=draft,
        )
        for draft in drafts
    ]
    chunks_to_process = _limit_chunks(chunks, max_chunks)
    extractor = CharacterSettingExtractor(model=model_name)

    print(
        "debug text analysis started "
        f"text_file={text_file} "
        f"episode_id={episode_id} "
        f"work_id={work_id} "
        f"analysis_job_id={analysis_job_id}",
        flush=True,
    )
    print(
        f"text chars raw={len(raw_text)} normalized={len(normalized_text)} "
        f"chunks={len(chunks)} processed_chunks={len(chunks_to_process)}",
        flush=True,
    )

    all_candidates: list[DebugCandidate] = []
    for chunk in chunks_to_process:
        draft = chunk.draft
        print(
            f"extracting chunk index={draft.chunk_index} "
            f"chunk_id={chunk.id} "
            f"offset={draft.start_offset}..{draft.end_offset} "
            f"chars={len(draft.chunk_text)} "
            f"paragraphs={draft.paragraph_start_index}..{draft.paragraph_end_index}",
            flush=True,
        )
        extraction_result = extractor.extract_from_chunk(
            source_chunk_id=chunk.id,
            chunk_text=draft.chunk_text,
            episode_no=episode_no,
            episode_title=episode_title,
        )
        resolved_candidates = resolve_candidate_evidence_offsets(
            candidates=extraction_result.candidates,
            chunk_text=draft.chunk_text,
            chunk_start_offset=draft.start_offset,
        )
        _print_candidate_preview(resolved_candidates)
        all_candidates.extend(
            DebugCandidate(
                episode_id=episode_id,
                work_id=work_id,
                analysis_job_id=analysis_job_id,
                candidate=candidate,
            )
            for candidate in resolved_candidates
        )

    result = _build_result(
        text_file=text_file,
        episode_id=episode_id,
        work_id=work_id,
        analysis_job_id=analysis_job_id,
        episode_no=episode_no,
        episode_title=episode_title,
        raw_text=raw_text,
        normalized_text=normalized_text,
        chunks=chunks,
        processed_chunks=chunks_to_process,
        candidates=all_candidates,
    )
    if output_json is not None:
        output_json.parent.mkdir(parents=True, exist_ok=True)
        output_json.write_text(
            json.dumps(result, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        print(f"debug result written output_json={output_json}", flush=True)

    print(json.dumps(result["summary"], ensure_ascii=False, indent=2), flush=True)
    return result


def main() -> None:
    args = _parse_args()
    run_episode_text_analysis_debug(
        text_file=args.text_file,
        episode_id=args.episode_id or uuid4(),
        work_id=args.work_id or uuid4(),
        analysis_job_id=args.analysis_job_id or uuid4(),
        episode_no=args.episode_no,
        episode_title=args.episode_title,
        model_name=args.model_name,
        max_chunks=args.max_chunks,
        output_json=args.output_json,
    )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Run one local episode text through chunking, LLM setting extraction, "
            "and evidence offset resolution without Spring, DB, or S3."
        )
    )
    parser.add_argument("--text-file", type=Path, required=True)
    parser.add_argument("--episode-id", type=UUID, default=None)
    parser.add_argument("--work-id", type=UUID, default=None)
    parser.add_argument("--analysis-job-id", type=UUID, default=None)
    parser.add_argument("--episode-no", type=int, default=1)
    parser.add_argument("--episode-title", default=None)
    parser.add_argument("--model-name", default=None)
    parser.add_argument(
        "--max-chunks",
        type=int,
        default=None,
        help="Limit processed chunks to reduce LLM cost during manual checks.",
    )
    parser.add_argument(
        "--output-json",
        type=Path,
        default=None,
        help="Write chunks and resolved candidates to a JSON file.",
    )
    return parser.parse_args()


def _limit_chunks(chunks: list[DebugChunk], max_chunks: int | None) -> list[DebugChunk]:
    if max_chunks is None:
        return chunks
    if max_chunks < 1:
        raise ValueError("--max-chunks must be at least 1.")
    return chunks[:max_chunks]


def _print_candidate_preview(candidates: list[ExtractedSettingCandidate]) -> None:
    print(f"  extracted_candidates={len(candidates)}", flush=True)
    for candidate in candidates:
        span_summary = ", ".join(
            _format_span(span.start_offset, span.end_offset, span.quote)
            for span in candidate.evidence_spans
        )
        print(
            "  - "
            f"{candidate.entity_name} "
            f"{candidate.attribute_name}={candidate.attribute_value} "
            f"value_type={candidate.value_type} "
            f"confidence={candidate.confidence} "
            f"spans=[{span_summary}]",
            flush=True,
        )


def _format_span(start_offset: int | None, end_offset: int | None, quote: str) -> str:
    location = "not-found" if start_offset is None or end_offset is None else f"{start_offset}..{end_offset}"
    return f"{location} quote={quote[:40]!r}"


def _build_result(
    text_file: Path,
    episode_id: UUID,
    work_id: UUID,
    analysis_job_id: UUID,
    episode_no: int,
    episode_title: str | None,
    raw_text: str,
    normalized_text: str,
    chunks: list[DebugChunk],
    processed_chunks: list[DebugChunk],
    candidates: list[DebugCandidate],
) -> dict:
    return {
        "summary": {
            "textFile": str(text_file),
            "episodeId": str(episode_id),
            "workId": str(work_id),
            "analysisJobId": str(analysis_job_id),
            "episodeNo": episode_no,
            "episodeTitle": episode_title,
            "rawCharCount": len(raw_text),
            "normalizedCharCount": len(normalized_text),
            "chunkCount": len(chunks),
            "processedChunkCount": len(processed_chunks),
            "candidateCount": len(candidates),
        },
        "chunks": [
            {
                "id": str(chunk.id),
                "chunkIndex": chunk.draft.chunk_index,
                "startOffset": chunk.draft.start_offset,
                "endOffset": chunk.draft.end_offset,
                "paragraphStartIndex": chunk.draft.paragraph_start_index,
                "paragraphEndIndex": chunk.draft.paragraph_end_index,
                "charCount": len(chunk.draft.chunk_text),
                "textPreview": chunk.draft.chunk_text[:160],
                "chunkText": chunk.draft.chunk_text,
            }
            for chunk in chunks
        ],
        "settingCandidates": [
            {
                "episodeId": str(debug_candidate.episode_id),
                "workId": str(debug_candidate.work_id),
                "analysisJobId": str(debug_candidate.analysis_job_id),
                **debug_candidate.candidate.model_dump(mode="json"),
                "evidenceMatches": [
                    _build_evidence_match(normalized_text, span.model_dump(mode="json"))
                    for span in debug_candidate.candidate.evidence_spans
                ],
            }
            for debug_candidate in candidates
        ],
    }


def _build_evidence_match(normalized_text: str, span: dict) -> dict:
    start_offset = span["start_offset"]
    end_offset = span["end_offset"]
    if start_offset is None or end_offset is None:
        return {
            "quote": span["quote"],
            "startOffset": None,
            "endOffset": None,
            "matchedText": None,
            "matched": False,
        }

    matched_text = normalized_text[start_offset:end_offset]
    return {
        "quote": span["quote"],
        "startOffset": start_offset,
        "endOffset": end_offset,
        "matchedText": matched_text,
        "matched": matched_text == span["quote"],
    }


if __name__ == "__main__":
    main()
