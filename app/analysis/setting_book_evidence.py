"""Ground BOOK evidence in raw source before it reaches publishing/identity memory."""

import re
from dataclasses import dataclass

from app.analysis.evidence_span_resolver import resolve_evidence_span_offsets
from app.analysis.schemas import ExtractedEvidenceSpan

BOOK_CURRENT_FUNCTION_INSTRUCTIONS = """
설정집의 명확한 현재 정상 기능도 독립적인 현재 정보입니다. 제공 schema에 대응하는
profile 항목이 있으면 그 정상 기능을 양의 현재 값으로 보존하세요. 회복했다는 말만으로
항상 이상 상태의 active=false 종료 후보 하나로 축소하지 마세요. 현재 정상 기능과 과거
이상 상태의 종료는 의미가 다릅니다. profile schema가 없고 status.*에 대응하면 현재
적용되는 정상 기능 자체를 나타내며, active는 그 정상 기능 후보의 적용 여부입니다.
기존 이상 STATUS가 실제 존재하고 원문에서 회복 결과가 관찰되면 그 종료 판단도 2차가
실제 대상을 보고 수행할 수 있게 결과 근거를 보존하세요. 없는 종료 대상은 만들지 않습니다.
치료 시도만 있고 기능 회복 결과가 없거나 과거/미래 가정이면 현재 정상 기능을 추론하지
마세요. active=false인 이상 상태 자체를 현재로 저장하거나 종료 정책을 우회하지 마세요.
""".strip()


class BookEvidenceError(ValueError):
    def __init__(self, candidate_index: int, span_index: int) -> None:
        self.candidate_index = candidate_index
        self.span_index = span_index
        super().__init__("BOOK_EVIDENCE_NOT_GROUNDED")


@dataclass(frozen=True)
class BookSection:
    heading: str
    start: int
    body_start: int
    end: int

    def subject_title(self) -> str | None:
        heading = self.heading.strip().strip("[]").lstrip("#").strip()
        if "/" not in heading:
            return None
        # Preserve source title semantics; the em-dash suffix is section metadata.
        return heading.split("/", 1)[1].split(" — ", 1)[0].strip()


def book_sections(text: str) -> list[BookSection]:
    headers = []
    offset = 0
    for line in text.splitlines(keepends=True):
        row = line.rstrip("\r\n")
        if re.fullmatch(r"\s*(?:\[[^\n]+\]|#{1,6}\s+.+)\s*", row):
            headers.append((row, offset, offset + len(line)))
        offset += len(line)
    return [BookSection(heading, start, body_start,
                        headers[index + 1][1] if index + 1 < len(headers) else len(text))
            for index, (heading, start, body_start) in enumerate(headers)]


def book_layout_data(text: str) -> list[dict]:
    return [{"heading": section.heading, "start_offset": section.start,
             "end_offset": section.end,
             "rows": text[section.body_start:section.end].splitlines()}
            for section in book_sections(text)]


def ground_setting_book_evidence(result, text: str, *, world: bool = False):
    from app.mappers.world_setting_candidate_mapper import normalize_world_setting_name

    sections = book_sections(text) if world else []
    candidates = []
    for index, candidate in enumerate(result.candidates):
        if not candidate.evidence_spans:
            raise BookEvidenceError(index, 0)
        spans = []
        parents = []
        for span_index, span in enumerate(candidate.evidence_spans):
            quote = span.quote
            if not quote.strip():
                raise BookEvidenceError(index, span_index)
            located = resolve_evidence_span_offsets(span, text, 0)
            if quote not in text:
                if located.start_offset is None or located.end_offset is None:
                    raise BookEvidenceError(index, span_index)
                quote = text[located.start_offset:located.end_offset]
            if not quote.strip() or quote not in text:
                raise BookEvidenceError(index, span_index)
            if world and len(book_sections(quote)) > 1:
                # Repeated exact quotes still have no unique offsets. They may
                # remain valid facts, but never cross independent book sections.
                raise BookEvidenceError(index, span_index)
            # A provider's offsets are not source-owned. The mapper will calculate
            # global offsets from the final exact quote; repetition stays unknown.
            spans.append(span.model_copy(update={"quote": quote, "start_offset": None, "end_offset": None}))
            if world and located.start_offset is not None:
                overlapping = [section for section in sections
                               if section.start < located.end_offset and section.end > located.start_offset]
                if len(overlapping) > 1:
                    # Use separate original spans for facts supported in several
                    # sections, never a whole-document quote as identity evidence.
                    raise BookEvidenceError(index, span_index)
                if len(overlapping) == 1:
                    parent = overlapping[0]
                    title = parent.subject_title()
                    if title is not None and normalize_world_setting_name(title) == normalize_world_setting_name(candidate.subject_name):
                        parents.append(parent.heading)
        for heading in dict.fromkeys(parents):
            if heading not in [span.quote for span in spans]:
                spans.append(ExtractedEvidenceSpan(quote=heading))
        candidates.append(candidate.model_copy(update={"evidence_spans": spans}))
    return result.model_copy(update={"candidates": candidates})


def book_evidence_retry_prompt(original: str, error: Exception) -> str:
    import json

    feedback = {"reason_code": "BOOK_RESPONSE_INVALID"}
    if isinstance(error, BookEvidenceError):
        feedback.update(reason_code="BOOK_EVIDENCE_NOT_GROUNDED",
                        candidate_index=error.candidate_index, span_index=error.span_index)
    payload = json.loads(original)
    payload["validation_feedback"] = feedback
    payload["response_correction"] = (
        "원문은 chunk_text 값만입니다. 모든 evidence quote를 그 원문에서 그대로 복사하고 "
        "주체 제목과 항목은 필요한 경우 별도 인용으로 남기세요. 표기나 제목을 바꾸거나 "
        "다른 부분을 이어 붙이지 마세요. 모든 필수 필드를 가진 전체 응답을 다시 반환하세요."
    )
    return json.dumps(payload, ensure_ascii=False)
