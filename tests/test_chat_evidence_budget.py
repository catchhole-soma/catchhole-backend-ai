"""Synthetic context-size regressions; no provider, embedding, DB, or manuscript.

Six moderate Korean passages exercise roughly 4,000 cl100k tokens, not the full
6,000-token boundary or every possible payload. The server-budget check is an
arithmetic contract check, not a Java ledger integration or measured API cost.
"""

from collections import Counter
from hashlib import sha256
import socket
from types import SimpleNamespace
from uuid import UUID

import pytest
import tiktoken

from app.chatbot.hybrid import _Evidence, _HybridSession
from app.chatbot.hybrid_items import group_items
from app.chatbot.hybrid_prompts import ANSWER_PROMPT
from app.chatbot.qa import encode, response_schema
from app.chatbot.qa_render import AnswerRow
from app.chatbot.replay import offline_settings
from app.retrieval.chunking import POLICY
from app.retrieval.evidence import compose_evidence
from app.retrieval.search_contracts import ChunkEvidence
from app.schemas.chat_hybrid import GroundedAnswer, HybridPlan
from app.schemas.setting_tools import SettingToolItem
from app.usage.metering import (
    _estimate_embedding_token_upper_bound,
    _estimate_text_token_upper_bound,
)
from tests.test_chat_focused_lookup import item
from tests.test_chat_hybrid import hybrid_settings


@pytest.fixture(autouse=True)
def no_external_connections(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Evidence budget checks must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def korean_chunks():
    sentence = (
        "합성 기록에서 세린은 훈련을 마친 뒤 새 기술을 배웠다. "
        "동료는 획득 과정과 당시 상황을 확인했다. "
    )
    chunks = []
    for index in range(6):
        episode = index // 3 + 1
        position = index % 3
        text = f"합성 장면 {index + 1}. " + sentence * 11
        chunks.append(
            ChunkEvidence(
                chunk_id=UUID(int=8100 + index),
                episode_id=UUID(int=8200 + episode),
                episode_no=episode,
                chunk_index=position,
                text=text,
                start_offset=position * 1000,
                end_offset=position * 1000 + len(text),
                chunk_hash=sha256(text.encode()).hexdigest(),
                source_key=f"synthetic/context-budget/{episode}",
                source_version="synthetic-v1",
                source_hash="a" * 64,
                policy=POLICY,
                methods=("KEYWORD",),
                score=1.0,
            )
        )
    return chunks


def answer_context():
    cfg = offline_settings()
    hcfg = hybrid_settings()
    execution = SimpleNamespace(
        context=SimpleNamespace(
            question="세린의 각 기술을 언제 어떻게 얻었는지 원문과 함께 알려줘",
            episode_from=None,
            episode_to=None,
            conversation=[],
            conversation_truncated=False,
        ),
        tools=lambda: None,
    )
    session = _HybridSession(SimpleNamespace(settings=cfg, hybrid=hcfg, metrics=None), execution)
    plan = HybridPlan.model_validate(
        {
            "disposition": "PROCEED",
            "registeredCurrentRequired": False,
            "rdbMode": "CURRENT",
            "sourceNeed": "PROCESS",
            "subjects": [
                {
                    "domain": "CHARACTER",
                    "name": "세린",
                    "focus": {"factType": "SKILL", "attribute": None},
                }
            ],
            "searchTerms": ["세린", "기술"],
            "includeEvidence": True,
            "episodeFrom": None,
            "episodeTo": None,
            "characterFilter": None,
            "perItem": True,
            "requestedFacets": ["TIME", "HOW"],
            "collection": None,
            "answerShape": "FACTS",
            "includeRegisteredValues": False,
            "eventScope": "ACTUAL",
        }
    )
    for index, name in enumerate(["잔광", "바람막이", "길잡이"], 1):
        row = item(701, "세린", category="SKILL", key=f"skill.synthetic-{index}", label=name)
        session.rows.append(AnswerRow(f"R{index}", SettingToolItem.model_validate(row)))
    session.chosen = list(session.rows)
    session.logical_items = group_items(session.rows)
    bundle = compose_evidence(
        korean_chunks(),
        max_items=hcfg.max_evidence,
        per_episode=hcfg.per_episode,
        max_tokens=hcfg.evidence_tokens,
    )
    session.evidences = [
        _Evidence(f"E{index}", chunk.text, chunk.episode_no, chunk=chunk)
        for index, chunk in enumerate(bundle.chunks, 1)
    ]
    return session, plan, bundle


def test_six_korean_passages_fit_new_evidence_budget_without_raising_item_caps():
    chunks = korean_chunks()
    cfg = hybrid_settings()
    assert cfg.evidence_tokens == 6000
    assert (cfg.max_evidence, cfg.per_episode) == (8, 3)
    encoding = tiktoken.get_encoding("cl100k_base")
    individual = [len(encoding.encode(chunk.text, disallowed_special=())) for chunk in chunks]
    assert all(600 <= count <= 800 for count in individual)
    assert 4000 <= sum(individual) <= 4300
    assert sorted(Counter(chunk.episode_id for chunk in chunks).values()) == [3, 3]

    previous = compose_evidence(
        chunks, max_items=cfg.max_evidence, per_episode=cfg.per_episode, max_tokens=3000
    )
    current = compose_evidence(
        chunks,
        max_items=cfg.max_evidence,
        per_episode=cfg.per_episode,
        max_tokens=cfg.evidence_tokens,
    )
    assert previous.limited and previous.token_count <= 3000
    assert len(previous.chunks) < 6
    assert {omission.reason for omission in previous.omissions} == {"TOKEN_LIMIT"}
    assert len(current.chunks) == len(current.passages) == 6
    assert current.token_count == sum(individual)
    assert current.omissions == () and not current.limited
    assert {chunk.text for chunk in current.chunks} == {chunk.text for chunk in chunks}


def test_actual_hybrid_answer_payload_fits_luna_input_with_all_six_passages():
    session, plan, bundle = answer_context()
    cfg = session.cfg
    assert (cfg.model, cfg.max_input_tokens, cfg.max_output_tokens) == (
        "gpt-6-luna",
        32000,
        2000,
    )
    payload = session.answer_payload(plan)
    schema = response_schema(GroundedAnswer)
    assert len(payload["passages"]) == 6
    assert len(payload["rows"]) == len(payload["items"]) == 3
    assert {passage["text"] for passage in payload["passages"]} == {
        chunk.text for chunk in bundle.chunks
    }
    assert session.fits(ANSWER_PROMPT, payload, schema)
    assert len(ANSWER_PROMPT) + len(encode(payload)) <= cfg.max_context_characters
    estimated = _estimate_text_token_upper_bound(
        ANSWER_PROMPT, encode(payload), cfg.model, 0, schema
    )
    reserved = _estimate_text_token_upper_bound(
        ANSWER_PROMPT, encode(payload), cfg.model, cfg.answer_max_output_tokens, schema
    )
    assert estimated <= cfg.max_input_tokens
    assert reserved == estimated + cfg.answer_max_output_tokens
    assert session.codes == []


def test_six_maximum_text_reservations_and_one_embedding_fit_server_budget_contract():
    cfg, hcfg = offline_settings(), hybrid_settings()
    assert (hcfg.max_model_calls, hcfg.max_query_embeddings) == (6, 1)
    # The orchestrator slices every query-embedding input to 1,000 Unicode code points.
    # Four-byte code points bound the existing UTF-8-based embedding reservation.
    query_upper_bound = "😀" * 1000
    embedding_reservation = _estimate_embedding_token_upper_bound([query_upper_bound])
    assert embedding_reservation == 4256
    # Includes truncation recovery within the same six text-call limit.
    maximum_text_reservation = cfg.max_input_tokens + cfg.truncation_retry_max_output_tokens
    maximum_attempt_reservation = (
        hcfg.max_model_calls * maximum_text_reservation
        + hcfg.max_query_embeddings * embedding_reservation
    )
    assert maximum_text_reservation == 38000
    assert maximum_attempt_reservation == 232256
    # Mirror the explicitly agreed Spring attempt budget, without requiring a sibling checkout.
    server_attempt_budget = 240000
    assert maximum_attempt_reservation <= server_attempt_budget
