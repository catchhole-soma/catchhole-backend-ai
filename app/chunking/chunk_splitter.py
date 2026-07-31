from dataclasses import dataclass

# 청크는 문단 경계를 우선해 약 2,500자로 묶고, 한 청크가 3,000자를 넘지 않게 한다.
# 마지막 청크와 짧은 회차는 1,000자보다 작을 수 있다.
DEFAULT_TARGET_CHARS = 2500
DEFAULT_MAX_CHARS = 3000
DEFAULT_MIN_CHARS = 1000

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
        # 원문 slice가 최대 길이를 넘는 경우 최소 길이와 무관하게 현재 청크를 먼저 확정한다.
        if current and candidate_length > max_chars:
            _flush_chunk(chunks, current, text)
            current = [paragraph]
            continue

        current.append(paragraph)
        # target에 도달한 뒤 확정해야 기본 청크가 2,500자 안팎의 문맥을 갖는다.
        if _combined_length(current) >= target_chars and _combined_length(current) >= min_chars:
            _flush_chunk(chunks, current, text)
            current = []

    _flush_chunk(chunks, current, text)
    return chunks

#텍스트를 문단 목록으로 바꾼다
def split_paragraphs(text: str) -> list[Paragraph]: #자바 문법으론 List<Paragraph> splitParagraphs(String text)
    # 문단 offset은 Episode.content_s3_key로 읽은 회차 원문 기준으로 계산한다.
    paragraphs: list[Paragraph] = []
    paragraph_index = 0 # 현재 몇 번째 문단인지 세는 값
    cursor = 0 # 원문 전체에서 현재 줄이 시작하는 위치

    for line in text.splitlines(keepends=True): # 줄바꿈 문자를 포함해 가져와 원문 cursor를 정확히 계산한다.
        if line.endswith("\r\n"):
            line_text = line[:-2]
        elif line.endswith(("\r", "\n")):
            line_text = line[:-1]
        else:
            line_text = line
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
    # 문단 사이의 CRLF, 빈 줄, 특수 공백도 실제 chunk_text에 포함되므로 원문 span으로 계산한다.
    return paragraphs[-1].end_offset - paragraphs[0].start_offset
