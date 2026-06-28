from dataclasses import dataclass

#청크 기준 정의 - 기본, 최대, 최소
DEFAULT_TARGET_CHARS = 1000
DEFAULT_MAX_CHARS = 1500
DEFAULT_MIN_CHARS = 300

#Java의 record와 유사한 형식 (현재는 한 파일의 내부 응답 경우에 사용 중)
@dataclass(frozen=True)
class Paragraph: #문단 하나를 표햔하는 객체
    index: int
    text: str
    start_offset: int
    end_offset: int

#Java의 record와 유사한 형식
@dataclass(frozen=True)
class EpisodeChunkDraft: #최종적으로 만들어지는 청크 하나
    chunk_index: int
    chunk_text: str
    start_offset: int
    end_offset: int
    paragraph_start_index: int
    paragraph_end_index: int


def split_into_chunks(
    text: str,
    target_chars: int = DEFAULT_TARGET_CHARS, # 값을 넘기지 않았다면 기본 값을 쓰겠다는 문법
    max_chars: int = DEFAULT_MAX_CHARS,
    min_chars: int = DEFAULT_MIN_CHARS,
) -> list[EpisodeChunkDraft]:
    paragraphs = split_paragraphs(text) # 원문을 문단 단위로 나눈 결과 List
    chunks: list[EpisodeChunkDraft] = [] # 최종 청크
    current: list[Paragraph] = [] # 현재 만들고 있는 청크에 들어갈 문단들

    # 웹소설 문맥을 보존하기 위해 문단 경계를 우선으로 chunk를 만든다.
    for paragraph in paragraphs:
        if len(paragraph.text) > max_chars: # 최대 보다 문단이 길다면
            _flush_chunk(chunks, current, text) # 지금까지 모은 문단들을 chunk로 확정
            current = []
            chunks.extend(_split_long_paragraph(paragraph, len(chunks), max_chars))
            continue

        candidate_length = _combined_length([*current, paragraph]) # 현재 청크에 이번 문단까지 넣었을 때 길이 계산
        # 지금 문단을 포함해서 1000자가 넘고, 지금 문단을 제외해도 300자가 된다면 청크 확정, 아니라면 이번 문단과 합치기
        if current and candidate_length > target_chars and _combined_length(current) >= min_chars:
            _flush_chunk(chunks, current, text)
            current = [paragraph]
            continue

        current.append(paragraph)

    _flush_chunk(chunks, current, text)
    return chunks

#텍스트를 문단 목록으로 바꾼다
def split_paragraphs(text: str) -> list[Paragraph]: #자바 문법으론 List<Paragraph> splitParagraphs(String text)
    # 문단 offset은 정규화된 회차 원문 기준으로 계산한다.
    paragraphs: list[Paragraph] = []
    paragraph_index = 0 # 현재 몇 번째 문단인지 세는 값
    cursor = 0 # 원문 전체에서 현재 줄이 시작하는 위치

    for line in text.splitlines(keepends=True): #줄단위로 나누는데 줄바꿈문자\n 까지 포함해서 가져간다(커서 계산의 정확성을 위해).
        line_text = line.rstrip("\n")
        if line_text.strip(): #공백만 있는게 아니라면
            start_offset = cursor # 현재 줄이 원문 전체에서 어디인지
            end_offset = cursor + len(line_text)
            paragraphs.append(
                Paragraph(
                    index=paragraph_index,
                    text=line_text,
                    start_offset=start_offset,
                    end_offset=end_offset,
                )
            ) #문단 하나 만들기
            paragraph_index += 1
        cursor += len(line)

    return paragraphs


def _flush_chunk(chunks: list[EpisodeChunkDraft], paragraphs: list[Paragraph], text: str) -> None:
    if not paragraphs:
        return

    # chunk_text는 재조립하지 않고 원문 slice를 그대로 사용해 근거 위치 계산을 보존한다.
    start_offset = paragraphs[0].start_offset
    end_offset = paragraphs[-1].end_offset
    chunks.append(
        EpisodeChunkDraft(
            chunk_index=len(chunks),
            chunk_text=text[start_offset:end_offset],
            start_offset=start_offset,
            end_offset=end_offset,
            paragraph_start_index=paragraphs[0].index,
            paragraph_end_index=paragraphs[-1].index,
        )
    )

# 한 문단이 지나치게 길면 문단 하나 안에서만 max_chars 단위로 나눈다.
def _split_long_paragraph(
    paragraph: Paragraph,
    start_chunk_index: int,
    max_chars: int,
) -> list[EpisodeChunkDraft]:
    chunks: list[EpisodeChunkDraft] = []
    # 0부터 문단 길이까지 max_chars 간격으로 숫자를 만들어.
    for chunk_index, start in enumerate(range(0, len(paragraph.text), max_chars), start_chunk_index):
        end = min(start + max_chars, len(paragraph.text))
        chunks.append(
            EpisodeChunkDraft(
                chunk_index=chunk_index,
                chunk_text=paragraph.text[start:end],
                start_offset=paragraph.start_offset + start,
                end_offset=paragraph.start_offset + end,
                paragraph_start_index=paragraph.index,
                paragraph_end_index=paragraph.index,
            )
        )
    return chunks


def _combined_length(paragraphs: list[Paragraph]) -> int:
    if not paragraphs:
        return 0
    return sum(len(paragraph.text) for paragraph in paragraphs) + len(paragraphs) - 1
