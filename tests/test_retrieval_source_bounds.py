"""Legacy stored originals use a byte read bound, not the current upload limit."""

from dataclasses import replace
from hashlib import sha256
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.retrieval.contracts import IndexSpec, SourceChanged
from app.retrieval.service import MAX_SOURCE_BYTES, RetrievalIndexService
from tests.test_retrieval_backfill import Corpus, config, invoke
from tests.test_retrieval_service import MemoryStore, Provider, run


def source_service(value, *, splitter=None):
    store = MemoryStore()
    store.source = replace(store.source, content_hash=sha256(value.encode("utf-8")).hexdigest())

    def read(key, *, version_id, max_bytes):
        assert key == store.source.key and version_id == store.source.version
        assert max_bytes == MAX_SOURCE_BYTES
        # Deliberately ignores the bound, exercising the service's own validation.
        return value

    return store, RetrievalIndexService(
        store, SimpleNamespace(get_text=read), IndexSpec(), splitter=splitter
    )


def test_large_legacy_original_keeps_unicode_offsets_hashes_and_existing_chunk_policy():
    sentence = "가나다라마바사.😀\r\n" + "synthetic scene " * 12 + "\r\n"
    value = sentence * 1400
    assert len(value) > 250000 and len(value.encode("utf-8")) < MAX_SOURCE_BYTES
    store, service = source_service(value)
    slices = service._slices(store.source)
    assert len(slices) > 1
    assert slices[0].start == 0 and slices[-1].end == len(value)
    for index, chunk in enumerate(slices):
        assert chunk.index == index
        assert chunk.text == value[chunk.start : chunk.end]
        assert chunk.content_hash == sha256(chunk.text.encode("utf-8")).hexdigest()
        assert chunk.tokens == service.splitter.count(chunk.text) <= 800
        if index:
            previous = slices[index - 1]
            assert previous.start < chunk.start <= previous.end < chunk.end
            assert service.splitter.count(value[chunk.start : previous.end]) <= 100
    assert store.writes == 0 and not store.operations


@pytest.mark.parametrize("byte_count", [MAX_SOURCE_BYTES - 1, MAX_SOURCE_BYTES])
def test_utf8_read_boundary_is_inclusive_and_independent_of_character_count(byte_count):
    value = "😀" * (byte_count // 4) + "a" * (byte_count % 4)
    assert len(value.encode("utf-8")) == byte_count
    assert len(value) > 250000
    split_values = []
    store, service = source_service(
        value, splitter=SimpleNamespace(split=lambda text: split_values.append(text) or [])
    )
    assert service._slices(store.source) == []
    assert split_values == [value]
    assert store.writes == 0 and not store.operations


def test_oversize_utf8_source_fails_before_prepare_or_provider_without_source_changed():
    from app.retrieval.contracts import Scope

    value = "😀" * (MAX_SOURCE_BYTES // 4) + "a"
    store, service = source_service(
        value, splitter=SimpleNamespace(split=lambda _: pytest.fail("Oversize source was split"))
    )
    scope, provider = Scope(store.source.work_id), Provider()
    with pytest.raises(ValueError, match="retrieval read limit"):
        service.prepare(scope)
    report = run(service, scope, provider)
    assert report["sourceFailures"] == [
        {"episodeId": str(store.source.episode_id), "code": "SOURCE_UNREADABLE"}
    ]
    assert report["providerAttempts"] == 0 and report["changedChunks"] == 0
    assert store.writes == 0 and not store.operations and not store.chunks
    preview = service.dry_run(scope)
    assert preview["sources"][0]["state"] == "SOURCE_UNREADABLE"
    assert preview["sources"][0]["inputTokens"] is None
    assert preview["knownInputTokens"] == 0 and preview["unknownSourceCount"] == 1


def test_real_hash_mismatch_remains_source_changed_before_prepare_or_provider():
    from app.retrieval.contracts import Scope

    store, service = source_service(
        "한글 원문\r\n😀",
        splitter=SimpleNamespace(split=lambda _: pytest.fail("Hash mismatch was split")),
    )
    store.source = replace(store.source, content_hash="b" * 64)
    scope = Scope(store.source.work_id)
    with pytest.raises(SourceChanged):
        service.prepare(scope)
    report = run(service, scope)
    assert report["sourceFailures"] == [
        {"episodeId": str(store.source.episode_id), "code": "SOURCE_CHANGED"}
    ]
    assert report["providerAttempts"] == 0 and store.writes == 0 and not store.operations


def test_large_legacy_backfill_resumes_missing_chunks_and_skips_saved_vectors(tmp_path):
    value = " " * 250001 + "\r\n아리아는 돌아왔습니다.😀\r\n" * 100
    corpus = Corpus()
    item = corpus.add(uuid4(), 1, value)
    cfg = config(tmp_path, max_chunks=1)
    first = invoke(cfg, corpus)
    assert first["state"] == "CHUNK_LIMIT" and first["embeddedChunks"] == 1
    assert first["checkedSources"] == 0 and not first["uncertainAttempt"]
    original_ids = [chunk.id for chunk in item.chunks]
    completed = invoke(replace(cfg, max_chunks=1000), corpus)
    assert completed["state"] == "READY" and completed["checkedSources"] == 1
    assert completed["embeddedChunks"] == len(original_ids) - 1
    assert original_ids == [chunk.id for chunk in item.chunks]
    assert all(chunk.status == "READY" for chunk in item.chunks)
    assert invoke(cfg, corpus)["providerAttempts"] == 0


def test_over_byte_limit_backfill_blocks_without_retrying_it_as_a_source_change(tmp_path):
    corpus = Corpus()
    corpus.add(uuid4(), 1, "😀" * (MAX_SOURCE_BYTES // 4) + "a")
    cfg = config(tmp_path)
    report = invoke(cfg, corpus)
    assert report["state"] == "BLOCKED" and report["reason"] == "SOURCE_UNREADABLE"
    assert report["providerAttempts"] == report["admittedInputTokenUpperBound"] == 0
    assert report["checkedSources"] == 0 and not report["uncertainAttempt"]
