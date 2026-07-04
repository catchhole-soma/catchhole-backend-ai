from dataclasses import dataclass

from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate


# 공백 정규화된 문자열과, 정규화된 각 문자가 원문에서 어느 범위였는지 함께 들고 있는 값 객체
@dataclass(frozen=True)
class _NormalizedText:
    # 공백이 정규화된 문자열
    text: str

    # 정규화된 text의 각 문자마다 원문 기준 (start, end) 범위를 저장
    # 예: 원문 "A   B" → 정규화 "A B"
    # 정규화된 공백 " " 하나는 원문 범위 (1, 4)를 가리킬 수 있다.
    ranges: list[tuple[int, int]]


def resolve_candidate_evidence_offsets(
    candidates: list[ExtractedSettingCandidate],
    chunk_text: str,
    chunk_start_offset: int,
) -> list[ExtractedSettingCandidate]:
    # 여러 setting candidate의 evidence_spans를 한 번에 보정한다.
    # candidate 자체를 직접 수정하지 않고, model_copy로 새 candidate를 만든다.
    return [
        candidate.model_copy(
            update={
                # candidate 안의 evidence_spans만 보정된 span 목록으로 교체한다.
                "evidence_spans": [
                    resolve_evidence_span_offsets(
                        span,
                        chunk_text=chunk_text,
                        chunk_start_offset=chunk_start_offset,
                    )
                    for span in candidate.evidence_spans
                ]
            }
        )
        for candidate in candidates
    ]


def resolve_evidence_span_offsets(
    span: ExtractedEvidenceSpan,
    chunk_text: str,
    chunk_start_offset: int,
) -> ExtractedEvidenceSpan:
    # LLM이 준 start_offset/end_offset은 틀릴 수 있으므로 믿지 않는다.
    # span.quote를 실제 chunk_text 안에서 다시 찾아 정확한 offset을 계산한다.

    # 1차: quote가 chunk_text에 완전히 똑같이 들어있는지 찾는다.
    exact_range = _find_exact_range(chunk_text, span.quote)

    # exact match에 성공하면 그 위치를 기준으로 offset을 채운다.
    if exact_range is not None:
        return _with_offsets(span, chunk_start_offset, exact_range)

    # 2차: 줄바꿈/연속 공백 차이 때문에 exact match가 실패할 수 있다.
    # 그래서 공백을 하나로 정규화한 뒤 다시 검색한다.
    normalized_range = _find_whitespace_normalized_range(chunk_text, span.quote)

    # 공백 정규화 검색에 성공하면 원문 기준 위치로 되돌려 offset을 채운다.
    if normalized_range is not None:
        return _with_offsets(span, chunk_start_offset, normalized_range)

    # exact match도 실패하고, 공백 정규화 match도 실패한 경우.
    # quote의 위치를 확정할 수 없으므로 offset을 None으로 비운다.
    return span.model_copy(update={"start_offset": None, "end_offset": None})


def _find_exact_range(chunk_text: str, quote: str) -> tuple[int, int] | None:
    # quote가 chunk_text 안에 그대로 존재하는지 찾는다.
    # str.find()는 찾으면 시작 인덱스를 반환하고, 못 찾으면 -1을 반환한다.
    start = chunk_text.find(quote)

    # quote를 찾지 못한 경우
    if start < 0:
        return None

    # Python의 slice 방식처럼 end는 마지막 글자의 다음 위치로 둔다.
    # 예: chunk_text[start:end]
    return start, start + len(quote)


def _find_whitespace_normalized_range(chunk_text: str, quote: str) -> tuple[int, int] | None:
    # 원문과 quote의 공백 차이를 무시하고 찾기 위한 함수.
    # 예:
    # chunk_text = "그는   검을\n들었다"
    # quote      = "그는 검을 들었다"
    # exact match는 실패하지만, 공백 정규화 후에는 매칭될 수 있다.

    # chunk_text를 공백 정규화하면서, 정규화 문자별 원문 범위도 기록한다.
    normalized_chunk_text = _normalize_whitespace_with_ranges(chunk_text)

    # quote도 같은 방식으로 공백 정규화한다.
    # quote 쪽은 위치 복원이 필요 없으므로 .text만 사용한다.
    normalized_quote = _normalize_whitespace_with_ranges(quote).text

    # quote가 공백뿐이거나 빈 문자열이면 찾을 수 없다고 본다.
    if not normalized_quote:
        return None

    # 정규화된 chunk_text 안에서 정규화된 quote를 찾는다.
    normalized_start = normalized_chunk_text.text.find(normalized_quote)

    # 정규화된 문자열에서도 찾지 못한 경우
    if normalized_start < 0:
        return None

    # 정규화된 문자열 기준 끝 위치
    normalized_end = normalized_start + len(normalized_quote)

    # 정규화 문자열의 시작 위치를 원문 위치로 되돌린다.
    # ranges[normalized_start]는 정규화된 첫 문자가 원문에서 차지한 범위.
    start = normalized_chunk_text.ranges[normalized_start][0]

    # 정규화 문자열의 마지막 문자가 원문에서 끝난 위치를 사용한다.
    # normalized_end는 exclusive라서 마지막 문자는 normalized_end - 1.
    end = normalized_chunk_text.ranges[normalized_end - 1][1]

    # 최종적으로 원문 chunk_text 기준의 (start, end)를 반환한다.
    return start, end


def _normalize_whitespace_with_ranges(text: str) -> _NormalizedText:
    # 모든 연속 공백을 공백 하나(" ")로 줄인다.
    # 동시에 정규화된 각 문자가 원문에서 어느 범위였는지 ranges에 저장한다.

    # 정규화된 문자를 하나씩 담을 리스트
    normalized_chars: list[str] = []

    # normalized_chars의 각 문자에 대응하는 원문 범위 리스트
    ranges: list[tuple[int, int]] = []

    # 현재 text에서 보고 있는 문자 위치
    index = 0

    # text를 앞에서부터 끝까지 순회한다.
    while index < len(text):
        # 현재 문자가 공백이면 탭, 줄바꿈, 스페이스 등을 모두 포함한다.
        if text[index].isspace():
            # 연속 공백이 시작된 원문 위치
            start = index

            # 공백이 아닌 문자가 나올 때까지 index를 전진시킨다.
            while index < len(text) and text[index].isspace():
                index += 1

            # 연속 공백 여러 개를 정규화 문자열에서는 공백 하나로 표현한다.
            normalized_chars.append(" ")

            # 이 정규화된 공백 하나가 원문에서는 start부터 index까지였다고 기록한다.
            ranges.append((start, index))

            # 이미 index를 다음 문자 위치로 옮겼으므로 아래 일반 문자 처리로 가지 않는다.
            continue

        # 공백이 아닌 일반 문자는 그대로 정규화 문자열에 넣는다.
        normalized_chars.append(text[index])

        # 일반 문자 하나는 원문에서도 index부터 index + 1 범위다.
        ranges.append((index, index + 1))

        # 다음 문자로 이동한다.
        index += 1

    # 문자 리스트를 하나의 문자열로 합쳐서 반환한다.
    return _NormalizedText(text="".join(normalized_chars), ranges=ranges)


def _with_offsets(
    span: ExtractedEvidenceSpan,
    chunk_start_offset: int,
    chunk_local_range: tuple[int, int],
) -> ExtractedEvidenceSpan:
    # chunk 내부 offset을 episode 전체 offset으로 변환한다.
    # chunk_local_range는 chunk_text 기준 위치이고,
    # chunk_start_offset을 더하면 회차 전체 원문 기준 위치가 된다.

    # tuple 언패킹.
    # 예: chunk_local_range = (20, 35)
    start, end = chunk_local_range

    # 기존 span을 직접 수정하지 않고,
    # start_offset/end_offset만 바꾼 새 span 객체를 반환한다.
    return span.model_copy(
        update={
            # 회차 전체 기준 시작 위치
            "start_offset": chunk_start_offset + start,

            # 회차 전체 기준 끝 위치.
            # Python slice처럼 end_offset은 exclusive 값이다.
            "end_offset": chunk_start_offset + end,
        }
    )
