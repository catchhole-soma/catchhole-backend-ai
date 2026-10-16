from hashlib import sha256
import json

import pytest

from app.retrieval.chunking import RetrievalSplitter


@pytest.mark.parametrize(
    ("text", "expected_digest"),
    [
        ("가.\r\n" * 4000, "28f372f47388bdbf842fc5f4c1ea0b8dc463614cb38097d6c90d56baa3b17ac5"),
        (
            "아리아😀는 멈췄다.\r\n두 번째 문장입니다! 셋째 문장.\n" * 250,
            "b1cdcd4ebdc46301a2902d87c36a7cb3d36c64ec6856736008bd6a09c474243e",
        ),
        (
            "가나다라마바사😀" * 1800,
            "2ae99d4a2b0686bf0343d89fc303389d963c58201268216ee6df658bc194aee6",
        ),
    ],
    ids=["dense-crlf", "mixed-sentences", "no-boundary"],
)
def test_boundary_lookup_keeps_previously_indexed_chunk_identity(text, expected_digest):
    # Captured from the pre-optimization splitter (3b8e574). Offsets, token counts
    # and hashes must stay identical so saved vectors remain reusable under v1.
    chunks = RetrievalSplitter().split(text)
    identity = [(c.index, c.start, c.end, c.tokens, c.content_hash) for c in chunks]
    encoded = json.dumps(identity, separators=(",", ":")).encode()
    assert sha256(encoded).hexdigest() == expected_digest


@pytest.fixture(scope="module")
def splitter():
    return RetrievalSplitter()


@pytest.mark.parametrize("value", ["", " \r\n\t"])
def test_blank_original_has_no_embedding_input(splitter, value):
    assert splitter.split(value) == []


@pytest.mark.parametrize(
    "value",
    [
        "아리아는 반지의 추적 마법을 알아챘다. 그녀는 그것을 강물에 던졌다.\n" * 130,
        "가나다라마바사😀🧙🏽‍♀️" * 300,
        "한 문단\r\n\r\n두 번째 문단입니다.\r셋째! 넷째?\n" * 120,
        "<|endoftext|>는 원고의 문자열이다. " * 200,
        "x" * 40000,
    ],
)
def test_token_cap_full_coverage_exact_original_offsets_and_overlap(splitter, value):
    chunks = splitter.split(value)
    assert len(chunks) > 1
    assert chunks[0].start == 0 and chunks[-1].end == len(value)
    for index, c in enumerate(chunks):
        assert c.index == index
        assert value[c.start : c.end] == c.text
        assert c.tokens == splitter.count(c.text) <= 800
        assert c.content_hash == sha256(c.text.encode()).hexdigest()
        assert "\ufffd" not in c.text
        if index:
            previous = chunks[index - 1]
            assert previous.start < c.start <= previous.end < c.end
            assert splitter.count(value[c.start : previous.end]) <= 100


def test_prefers_real_sentence_boundary_without_normalizing_original(splitter):
    text = "아리아는 멈췄다.\r\n" * 200
    chunks = splitter.split(text)
    assert all(c.text.endswith("\r\n") for c in chunks)
    assert [c.content_hash for c in chunks] == [c.content_hash for c in splitter.split(text)]


def test_long_whitespace_never_becomes_an_empty_embedding_input(splitter):
    text = "\n" * 10000 + "아리아는 반지를 버렸다."
    chunks = splitter.split(text)
    assert chunks and all(c.text.strip() for c in chunks)
    assert chunks[-1].end == len(text)


@pytest.mark.parametrize("spaces", [25000, 249000])
def test_discarded_space_windows_make_progress_and_preserve_later_text(monkeypatch, spaces):
    splitter = RetrievalSplitter()
    count = splitter.count
    calls = 0

    def bounded_count(value):
        nonlocal calls
        calls += 1
        # Catch repeated tokenization without a slow, machine-dependent timing assertion.
        assert calls <= 256, "Discarded space windows repeatedly retain overlap"
        return count(value)

    monkeypatch.setattr(splitter, "count", bounded_count)
    text = " " * spaces + "\r\n아리아는 반지를 버렸다.😀"
    chunks = splitter.split(text)
    assert len(chunks) == 1
    chunk = chunks[0]
    assert not text[: chunk.start].strip()
    assert chunk.end == len(text)
    assert chunk.text == text[chunk.start : chunk.end]
    assert chunk.text.strip() == text.strip()
    assert chunk.tokens == count(chunk.text) <= 800
    assert chunk.content_hash == sha256(chunk.text.encode()).hexdigest()
