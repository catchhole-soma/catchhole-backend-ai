from app.chunking.chunk_splitter import (
    DEFAULT_MAX_CHARS,
    DEFAULT_TARGET_CHARS,
    split_into_chunks,
    split_paragraphs,
)


def test_split_paragraphs_keeps_offsets_from_episode_source() -> None:
    text = "첫 문단입니다.\n\n두 번째 문단입니다.\n세 번째 문단입니다."

    paragraphs = split_paragraphs(text)

    assert [paragraph.index for paragraph in paragraphs] == [0, 1, 2]
    assert paragraphs[0].start_offset == 0
    assert paragraphs[0].end_offset == len("첫 문단입니다.")
    assert text[paragraphs[1].start_offset : paragraphs[1].end_offset] == "두 번째 문단입니다."


def test_split_into_chunks_groups_paragraphs_with_source_positions() -> None:
    text = (
        "첫 번째 문단입니다.\n"
        "두 번째 문단입니다.\n"
        "세 번째 문단입니다.\n"
        "네 번째 문단입니다."
    )

    chunks = split_into_chunks(text, target_chars=25, max_chars=50, min_chars=10)

    assert len(chunks) == 2
    assert chunks[0].chunk_index == 0
    assert chunks[0].chunk_text == "첫 번째 문단입니다.\n두 번째 문단입니다.\n세 번째 문단입니다."
    assert chunks[0].paragraph_start_index == 0
    assert chunks[0].paragraph_end_index == 2
    assert text[chunks[0].start_offset : chunks[0].end_offset] == chunks[0].chunk_text
    assert chunks[1].chunk_index == 1
    assert chunks[1].paragraph_start_index == 3
    assert chunks[1].paragraph_end_index == 3


def test_split_into_chunks_splits_long_paragraph() -> None:
    text = "가" * 12

    chunks = split_into_chunks(text, target_chars=10, max_chars=5, min_chars=1)

    assert [chunk.chunk_text for chunk in chunks] == ["가" * 5, "가" * 5, "가" * 2]
    assert [(chunk.start_offset, chunk.end_offset) for chunk in chunks] == [(0, 5), (5, 10), (10, 12)]
    assert [chunk.chunk_index for chunk in chunks] == [0, 1, 2]


def test_split_into_chunks_keeps_blank_lines_inside_source_slice() -> None:
    text = "첫 번째 문단입니다.\n\n두 번째 문단입니다."

    chunks = split_into_chunks(text, target_chars=100, max_chars=150, min_chars=10)

    assert len(chunks) == 1
    assert chunks[0].chunk_text == text
    assert text[chunks[0].start_offset : chunks[0].end_offset] == chunks[0].chunk_text


def test_split_into_chunks_preserves_source_control_characters_and_offsets() -> None:
    text = "\ufeff첫 문장입니다.\r\n두 번째\t문장입니다.\u00a0 \r\n\r\n세 번째 문장입니다.\u200b"

    chunks = split_into_chunks(text)

    assert len(chunks) == 1
    assert chunks[0].chunk_text == text
    assert text[chunks[0].start_offset : chunks[0].end_offset] == text


def test_default_chunk_policy_targets_6000_and_caps_at_7000_characters() -> None:
    paragraph = "가" * 480
    text = "\n".join(paragraph for _ in range(30))

    chunks = split_into_chunks(text)

    assert DEFAULT_TARGET_CHARS == 6000
    assert DEFAULT_MAX_CHARS == 7000
    assert len(chunks) == 3
    assert all(len(chunk.chunk_text) <= DEFAULT_MAX_CHARS for chunk in chunks)
    assert all(
        len(chunk.chunk_text) >= DEFAULT_TARGET_CHARS
        for chunk in chunks[:-1]
    )
    assert all(
        text[chunk.start_offset : chunk.end_offset] == chunk.chunk_text
        for chunk in chunks
    )


def test_default_chunk_policy_caps_combined_source_slice_with_blank_lines() -> None:
    text = ("가" * 1600) + ("\r\n" * 20) + ("나" * 6500)

    chunks = split_into_chunks(text)

    assert [len(chunk.chunk_text) for chunk in chunks] == [1600, 6500]
    assert all(len(chunk.chunk_text) <= DEFAULT_MAX_CHARS for chunk in chunks)
