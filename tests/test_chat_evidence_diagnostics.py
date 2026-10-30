"""Offline evidence admission and presentation regressions, not model quality scores."""

import asyncio
from collections import Counter
from dataclasses import replace
import json
import logging
import socket
from uuid import UUID

import pytest

from app.chatbot.hybrid import _Evidence
from app.chatbot.hybrid_metrics import HybridMetrics
from app.chatbot.qa_render import AnswerRow, notices
from app.schemas.setting_tools import DirectEvidence, EvidenceQuote, SourceReference
from tests.test_chat_evidence_budget import answer_context, korean_chunks
from tests.test_chat_hybrid import replay, scenario
from tests.test_chat_hybrid_items import passage


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Evidence diagnostics must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def session_with(chunks=(), **limits):
    session, plan, _ = answer_context()
    # 생략 진단 회귀는 상한 도달 산술을 검사하므로 예전 8개·회차당 3개·6,000토큰 예산을 고정한다.
    session.hcfg = session.hcfg.model_copy(
        update={"max_evidence": 8, "per_episode": 3, "evidence_tokens": 6000, **limits}
    )
    session.metrics = HybridMetrics()
    session.evidences = [
        _Evidence(f"E{index}", chunk.text, chunk.episode_no, chunk=chunk)
        for index, chunk in enumerate(chunks, 1)
    ]
    session.evidence_registry = {e.ref: e for e in session.evidences}
    return session, plan


def omission_counts(metrics):
    return {
        (sample.labels["stage"], sample.labels["reason"]): sample.value
        for family in metrics.evidence_omissions.collect()
        for sample in family.samples
        if sample.name == "chat_retrieval_evidence_omissions_total"
    }


def direct_reference(number):
    return SourceReference(tool="CHARACTER_EVIDENCE", target_id=UUID(int=9100 + number))


@pytest.mark.parametrize("different_chunk_id", [False, True])
def test_duplicate_only_item_search_at_capacity_does_not_add_omission(different_chunk_id):
    chunks = [passage(f"합성 기록 {n}", n, episode=n // 3, offset=n * 100) for n in range(8)]
    session, _ = session_with(chunks)
    before = session.evidence_identity()
    assert len(before) == session.hcfg.max_evidence

    repeated = replace(
        chunks[-1],
        methods=("VECTOR",),
        chunk_id=UUID(int=9999) if different_chunk_id else chunks[-1].chunk_id,
    )
    session.add_item_evidence([chunks[0], repeated])

    assert session.evidence_identity() == before
    assert omission_counts(session.metrics) == {}
    assert session.codes == []


def test_rejected_candidates_do_not_consume_remaining_episode_quota():
    retained = [
        passage("가", 1, episode=1, offset=0),
        passage("나", 2, episode=1, offset=100),
        passage("다", 3, episode=2, offset=0),
    ]
    session, _ = session_with(retained, evidence_tokens=200)
    too_large = replace(passage("가 " * 210, 4, episode=1, offset=1000), score=10)
    invalid = replace(passage("비공개 합성 오류", 5, episode=2, offset=1000), chunk_hash="0" * 64)
    valid_a = passage("라", 6, episode=1, offset=2000)
    valid_b = passage("마", 7, episode=2, offset=2000)

    session.add_item_evidence([too_large, invalid, valid_a, valid_b], context_slots=5)

    assert {e.chunk.chunk_id for e in session.evidences} == {
        c.chunk_id for c in [*retained, valid_a, valid_b]
    }
    assert sorted(Counter(e.chunk.episode_id for e in session.evidences).values()) == [2, 3]
    assert omission_counts(session.metrics) == {
        ("item_search", "INVALID_CHUNK"): 1,
        ("item_search", "TOKEN_LIMIT"): 1,
    }


def test_retained_and_new_overlapping_passages_share_the_same_token_budget():
    original = passage("각" * 1750, 1, episode=1, offset=0)
    neighbor = passage("각" * 1750, 2, episode=1, offset=1)
    session, _ = session_with([original])
    assert session.evidence_token_count(session.evidences) == 3500

    session.add_item_evidence([neighbor])

    assert len(session.evidences) == 2
    assert session.evidence_token_count(session.evidences) == 3502
    assert omission_counts(session.metrics) == {}
    assert session.codes == []


@pytest.mark.parametrize("direct_quote, selected, total", [(None, 8, 5448), ("각" * 500, 7, 5405)])
def test_direct_and_search_passages_share_six_thousand_token_budget(direct_quote, selected, total):
    chunks = korean_chunks()
    session, plan = session_with()
    session.evidences = [
        _Evidence(
            f"E{n}", direct_quote or chunk.text, chunk.episode_no, direct_ref=direct_reference(n)
        )
        for n, chunk in enumerate(chunks[:2], 1)
    ]

    session.add_search_evidence(chunks)

    assert len(session.evidences) == selected
    assert sum(bool(e.direct_ref) for e in session.evidences) == 2
    assert session.evidence_token_count(session.evidences) == total
    assert session.evidence_token_count(session.evidences) <= session.hcfg.evidence_tokens == 6000
    assert len(session.answer_payload(plan)["passages"]) == selected
    assert omission_counts(session.metrics) == (
        {("search", "TOKEN_LIMIT"): 1} if direct_quote else {}
    )


def test_initial_direct_quotes_are_admitted_within_shared_budget():
    session, _ = session_with()
    template = session.rows[0].item
    session.chosen = []
    for n in range(8):
        evidence = DirectEvidence(
            status="AVAILABLE",
            candidate_id=None,
            episode_id=UUID(int=9200 + n),
            source_episode_no=n + 1,
            source_chunk_id=None,
            source_hash="a" * 64,
            source_version="synthetic-v1",
            matches_current_source=True,
            quotes=[EvidenceQuote(text="각" * 500, start_offset=0, end_offset=500)],
            truncated=False,
        )
        source_item = template.model_copy(update={"kind": "EVIDENCE", "evidence": evidence})
        row = AnswerRow(f"R{n}", template)
        row.evidence = [(direct_reference(n), source_item)]
        session.chosen.append(row)

    session.add_direct_evidence()

    assert len(session.evidences) == 6
    assert session.evidence_token_count(session.evidences) == 6000
    assert omission_counts(session.metrics) == {("direct", "TOKEN_LIMIT"): 2}
    assert set(session.codes) == {"EVIDENCE_CONTEXT_LIMIT"}


def test_composition_diagnostics_only_log_fixed_reasons_and_aggregate_counts(caplog):
    session, _ = session_with(evidence_tokens=200)
    secret = "DO_NOT_LOG_SOURCE_CONTENT"
    too_large = passage((secret + " 가 ") * 80, 1, episode=1)
    invalid = replace(passage(secret, 2, episode=2), chunk_hash="0" * 64)
    valid = passage("확인 가능한 합성 기록", 3, episode=2, offset=100)

    with caplog.at_level(logging.INFO, logger="app.chatbot.hybrid"):
        session.add_item_evidence([too_large, invalid, valid])

    assert omission_counts(session.metrics) == {
        ("item_search", "INVALID_CHUNK"): 1,
        ("item_search", "TOKEN_LIMIT"): 1,
    }
    records = [r for r in caplog.records if r.name == "app.chatbot.hybrid"]
    assert len(records) == 1
    record = records[0]
    assert record.args[:4] == ("item_search", 3, 0, 1)
    assert isinstance(record.args[4], int)
    assert record.args[5] == {"INVALID_CHUNK": 1, "TOKEN_LIMIT": 1}
    samples = [s for f in session.metrics.evidence_omissions.collect() for s in f.samples]
    assert all(set(sample.labels) == {"stage", "reason"} for sample in samples)
    diagnostic_output = (
        record.getMessage() + repr(record.args) + json.dumps([sample.labels for sample in samples])
    )
    for chunk in [too_large, invalid, valid]:
        for private in [chunk.text, str(chunk.chunk_id), str(chunk.episode_id), chunk.source_key]:
            assert private not in diagnostic_output
    assert session.execution.context.question not in diagnostic_output
    assert secret not in diagnostic_output


@pytest.mark.parametrize("coverage", ["COMPLETE", "PARTIAL"])
def test_success_omits_blanket_disclaimer_but_preserves_gaps_and_index_warning(tmp_path, coverage):
    case = scenario()
    case["readiness"] = "partial"
    case["outputs"][1]["coverage"] = coverage

    def limited_search(search):
        search.limited = True
        search.keyword_chunks.extend(
            passage(f"추가 합성 검색 기록 {n}", n, episode=10 + n) for n in range(9)
        )

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            # 생략 2건 산술은 예전 8개 상한 기준이다.
            hybrid={"max_rounds": 1, "max_evidence": 8, "per_episode": 3},
            search_setup=limited_search,
        )
    )

    output = result["finish"]["result"]
    assert output["outcome"] == "ANSWER"
    assert "관련 자료를 모두 검토한 결과는 아니에요" not in output["text"]
    assert ("질문의 일부만 답할 수 있었어요" in output["text"]) == (coverage == "PARTIAL")
    assert "근거 1" in output["text"] and len(output["retrievalSources"]) == 1
    assert notices(["INDEX_PARTIAL"])[0] in output["limitations"]
    assert omission_counts(result["metrics"])[("search", "ITEM_LIMIT")] == 2
    assert not set(notices(["SEARCH_LIMIT", "EVIDENCE_CONTEXT_LIMIT", "PARTIAL_ANSWER"])) & set(
        output["limitations"]
    )
    assert all(code not in output["text"] for code in ["SEARCH_LIMIT", "PARTIAL_ANSWER", "E1"])


def test_source_change_is_not_hidden_by_search_scope_presentation(tmp_path):
    def change_source(count, spring, search):
        if count == 2:
            search.changed = True

    result = asyncio.run(
        replay(
            tmp_path,
            search_setup=lambda search: setattr(search, "limited", True),
            model_hook=change_source,
        )
    )

    output = result["finish"]["result"]
    assert output["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert notices(["SOURCE_CHANGED"])[0] in output["limitations"]
    assert not output.get("retrievalSources")
    assert "강물" not in output["text"]
