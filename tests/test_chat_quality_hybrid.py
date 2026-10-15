"""Synthetic route/coverage/budget regressions, never a provider quality score.

The planner and answer outputs are authored inputs. Real orchestration, model
schema validation, metering, source revalidation and the SQLite outbox execute;
model inference and the external network do not.
"""

import asyncio
import copy
from dataclasses import replace
from hashlib import sha256
import socket

import pytest

from app.chatbot.qa import response_schema
from app.chatbot.qa_render import NOTICES
from app.schemas.chat_hybrid import GroundedAnswer, HybridPlan
from app.schemas.chat_qa import FieldSelection
from tests.test_chat_focused_lookup import catalog_subject, item, uid
from tests.test_chat_hybrid import replay, scenario, source_chunk


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Hybrid quality regressions cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


SKILLS = ["잔광", "바람막이", "길잡이"]
USAGE_TEXT = "세린은 잔광으로 동굴을 비추고 바람막이를 펼쳤다."


def skills(count=3, *, value_length=0):
    names = SKILLS if count == 3 else [f"합성기술{n:03}" for n in range(1, count + 1)]
    return [
        item(
            701,
            "세린",
            category="SKILL",
            key=f"skill.synthetic-{n:03}",
            label=name,
            value=("합성 설명 " * value_length)[:1900] if value_length else name,
        )
        for n, name in enumerate(names, 1)
    ]


def skill_case(*, per_item=True):
    case = scenario()
    case.update(question="세린의 각 스킬은 언제 어떻게 얻었어?", keyword=["ring"], vector=[])
    case["outputs"] = [
        {
            "disposition": "PROCEED",
            "rdbMode": "CURRENT",
            "sourceNeed": "PROCESS",
            "subjects": [
                {
                    "domain": "CHARACTER",
                    "name": "세린",
                    "focus": {"factType": "SKILL", "attribute": None},
                }
            ],
            "searchTerms": ["세린", "스킬"],
            "includeEvidence": True,
            "episodeFrom": None,
            "episodeTo": None,
            "characterFilter": None,
            "perItem": per_item,
            "requestedFacets": ["TIME", "HOW"],
        }
    ]
    return case


def usage_answer(*, refs=("R1", "R2"), unanswered=("R1", "R2", "R3"), coverage="PARTIAL"):
    return {
        "coverage": coverage,
        "claims": [
            {
                "text": "잔광과 바람막이를 사용하는 장면은 확인되지만 획득 경위는 확인되지 않습니다.",
                "evidenceRefs": ["E1"],
                "rowRefs": list(refs),
            }
        ],
        "conflicts": [],
        "unansweredRowRefs": list(unanswered),
    }


def item_answer(count=3, *, text=None, ref="E1", facet="HOW"):
    return {
        "coverage": "PARTIAL" if text else "NONE",
        "claims": [],
        "conflicts": [],
        "itemAnswers": [
            {
                "itemRef": f"I{n}",
                "answers": [{"facet": facet, "text": text, "evidenceRefs": [ref]}]
                if text and n == 1
                else [],
                "missingFacets": [
                    f for f in ["TIME", "HOW"] if not (text and n == 1 and f == facet)
                ],
            }
            for n in range(1, count + 1)
        ],
    }


def setup_catalog(rows):
    def setup(spring):
        spring.catalog = [catalog_subject(701, "세린", current=copy.deepcopy(rows))]

    return setup


def setup_source(search):
    set_source_text(search, USAGE_TEXT)


def set_source_text(search, text):
    search.keyword_chunks = [
        replace(
            source_chunk("ring"),
            text=text,
            episode_no=12,
            end_offset=len(text),
            chunk_hash=sha256(text.encode()).hexdigest(),
        )
    ]


def finished(result):
    assert result["finish"]["failure"] is None, result["finish"]
    return result["finish"]["result"]


def test_each_skill_is_retained_when_sources_only_prove_use_not_acquisition(tmp_path):
    case = skill_case()
    case["outputs"].append(item_answer())
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills()),
            search_setup=setup_source,
            hybrid={"max_rounds": 1},
            config={"max_selected_rows": 1},
        )
    )
    actual = finished(result)
    assert actual["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert all(name in actual["text"] for name in SKILLS)
    assert actual["text"].count("확정하기 어렵습니다") == 1
    assert all(actual["text"].count(name) == 1 for name in SKILLS)
    assert "12화에 얻" not in actual["text"]
    assert "R1" not in actual["text"] and "E1" not in actual["text"]
    assert not actual.get("retrievalSources")
    assert USAGE_TEXT not in actual["text"]
    assert NOTICES["NO_SEARCH_EVIDENCE"] in actual["limitations"]
    assert len(result["raw"].requests) == 2  # Planner + grounded answer; no lossy row selector.
    assert [r["rowRef"] for r in result["raw"].requests[1]["input"]["rows"]] == ["R1", "R2", "R3"]
    current = [q for q in result["spring"].queries if q["tool"] == "CHARACTER_CURRENT"]
    assert len(current) == 2
    assert current[0]["factType"] == "SKILL"
    assert current[1]["targetId"] == current[0]["targetId"]
    assert current[1]["factType"] is None and current[1]["size"] == 1
    assert "revalidate" not in result["search"].calls
    assert len(result["spring"].receipts) == 1
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2


@pytest.mark.parametrize("coverage", ["COMPLETE", "PARTIAL"])
def test_omitted_skill_cannot_be_hidden_by_declaring_answer_coverage(tmp_path, coverage):
    case = skill_case()
    bad = usage_answer(refs=("R1", "R2"), unanswered=(), coverage=coverage)
    case["outputs"].extend([bad, bad])
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills()),
            search_setup=setup_source,
        )
    )
    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert response["sources"] == response.get("retrievalSources", []) == []
    assert all(claim["text"] not in response["text"] for claim in bad["claims"])
    assert len(result["raw"].requests) == 3
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 4
    assert len(result["embedding"].inputs) == 1
    assert result["spring"].reports[-1]["outputTokens"] == 0
    assert "revalidate" not in result["search"].calls


@pytest.mark.parametrize("location", ["claim", "unanswered", "conflict"])
def test_unobserved_setting_reference_is_rejected_in_every_answer_channel(tmp_path, location):
    case = skill_case()
    bad = usage_answer()
    if location == "claim":
        bad["claims"][0]["rowRefs"].append("R999")
    elif location == "unanswered":
        bad["unansweredRowRefs"].append("R999")
    else:
        bad["conflicts"] = [{"rowRefs": ["R999"], "evidenceRefs": ["E1"]}]
    case["outputs"].extend([bad, bad])
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills()),
            search_setup=setup_source,
        )
    )
    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert response["sources"] == response.get("retrievalSources", []) == []
    assert all(claim["text"] not in response["text"] for claim in bad["claims"])
    assert "R999" not in response["text"]
    assert len(result["raw"].requests) == 3


def test_corrected_coverage_retry_preserves_all_skills_and_is_metered(tmp_path):
    case = skill_case()
    case["outputs"].extend(
        [
            usage_answer(refs=("R1",), unanswered=(), coverage="COMPLETE"),
            item_answer(),
        ]
    )
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills()),
            search_setup=setup_source,
            hybrid={"max_rounds": 1},
        )
    )
    assert all(name in finished(result)["text"] for name in SKILLS)
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 3
    assert len({r["callId"] for r in result["spring"].reservations}) == 3


@pytest.mark.parametrize(
    "count,value_length,config",
    [
        (101, 0, {"page_size": 50, "max_context_characters": 64000, "max_input_tokens": 32000}),
        (3, 400, {"max_context_characters": 6000}),
    ],
)
def test_oversized_per_item_request_clarifies_before_search_or_answer_generation(
    tmp_path,
    count,
    value_length,
    config,
):
    result = asyncio.run(
        replay(
            tmp_path,
            skill_case(),
            spring_setup=setup_catalog(skills(count, value_length=value_length)),
            search_setup=setup_source,
            config=config,
        )
    )
    actual = finished(result)
    assert actual["outcome"] == "CLARIFICATION"
    assert "항목이나 유형을 좁혀" in actual["text"]
    assert NOTICES["CONTEXT_LIMIT"] in actual["limitations"]
    assert result["search"].calls == [] and result["embedding"].inputs == []
    assert len(result["raw"].requests) == 1
    assert not result["spring"].receipts


@pytest.mark.parametrize("changed", [False, True])
def test_focused_pagination_leaves_budget_to_revalidate_partial_answer(tmp_path, changed):
    case = skill_case()
    draft = item_answer(6, text="스승에게 합성기술001을 배웠습니다.")
    case["outputs"].append(draft)

    def after_generation(count, spring, search):
        if count == 2:
            search.changed = changed

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_catalog(skills(8)),
            search_setup=lambda search: set_source_text(
                search, "세린은 스승에게 합성기술001을 배웠다."
            ),
            config={"page_size": 1, "max_tool_calls": 12},
            model_hook=after_generation,
        )
    )
    actual = finished(result)
    assert result["search"].calls == ["keyword", "revalidate"]
    # Find + six pages + final current check; the same 12-call budget also
    # preserves search, receipt, source revalidation and the final receipt.
    assert len(result["spring"].queries) == 8
    current = [q for q in result["spring"].queries if q["tool"] == "CHARACTER_CURRENT"]
    assert [q["page"] for q in current[:-1]] == list(range(6))
    assert all(q["factType"] == "SKILL" for q in current[:-1])
    assert current[-1]["targetId"] == current[0]["targetId"]
    assert current[-1]["factType"] is None and current[-1]["size"] == 1
    assert len(result["raw"].requests[-1]["input"]["items"]) == 6
    assert (
        len(result["spring"].queries) + len(result["search"].calls) + len(result["spring"].receipts)
    ) <= 12
    assert len(result["raw"].requests) == 2
    assert result["embedding"].inputs == []
    if changed:
        assert actual["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert not actual.get("retrievalSources", [])
        assert len(result["spring"].receipts) == 1
    else:
        assert actual["outcome"] == "ANSWER"
        assert "합성기술006" in actual["text"]
        assert "합성기술007" not in actual["text"] and "합성기술008" not in actual["text"]
        assert NOTICES["PARTIAL_RESULTS"] in actual["limitations"]
        assert NOTICES["PARTIAL_ANSWER"] not in actual["limitations"]
        assert "관련 자료를 모두 검토한 결과는 아니에요" not in actual["text"]
        assert "확인하지 못" in actual["text"]
        assert len(result["spring"].receipts) == 2
        assert result["spring"].receipts[0]["sources"] == result["spring"].receipts[1]["sources"]


@pytest.mark.parametrize("changed", [False, True])
def test_direct_partial_survives_search_budget_stop_and_rechecks_its_original_source(
    tmp_path, changed
):
    case = skill_case()
    direct_partial = item_answer(text="잔광을 스승에게 배웠습니다.")
    case["outputs"].extend(
        [
            direct_partial,
            item_answer(),
        ]
    )

    def setup(spring):
        rows = skills()
        direct = []
        for n, row in enumerate(rows, 1):
            reference = {"tool": "CHARACTER_EVIDENCE", "targetId": uid(800 + n)}
            row["sources"] = [reference]
            quote = f"세린은 스승에게 {row['displayName']}을 배웠다."
            source = copy.deepcopy(row)
            source.update(
                kind="EVIDENCE",
                id=reference["targetId"],
                evidence={
                    "status": "AVAILABLE",
                    "candidateId": uid(1800 + n),
                    "episodeId": uid(1900 + n),
                    "sourceEpisodeNo": 10 + n,
                    "sourceChunkId": None,
                    "sourceHash": "b" * 64,
                    "sourceVersion": "v1",
                    "matchesCurrentSource": True,
                    "quotes": [{"text": quote, "startOffset": 0, "endOffset": len(quote)}],
                    "truncated": False,
                },
            )
            direct.append(source)
        subject = catalog_subject(701, "세린", current=rows)
        subject["evidence"] = direct
        spring.catalog = [subject]

    def after_search_answer(count, spring, search):
        if changed and count == 3:
            spring.catalog[0]["evidence"][0]["evidence"]["quotes"][0]["text"] = "수정된 합성 원문"

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup,
            search_setup=setup_source,
            config={"max_tool_calls": 12},
            model_hook=after_search_answer,
        )
    )
    actual = finished(result)
    assert result["search"].calls == ["keyword", "keyword", "neighbors"]
    assert len(result["raw"].requests) == 3 and result["embedding"].inputs == []
    direct_queries = [q for q in result["spring"].queries if q["tool"] == "CHARACTER_EVIDENCE"]
    assert [q["targetId"] for q in direct_queries].count(uid(801)) == 2
    assert direct_queries[-1]["targetId"] == uid(801)
    current = [q for q in result["spring"].queries if q["tool"] == "CHARACTER_CURRENT"]
    assert len(current) == 2
    assert current[-1]["targetId"] == current[0]["targetId"]
    assert current[-1]["size"] == 1 and current[-1]["factType"] is None
    assert (
        len(result["spring"].queries) + len(result["search"].calls) + len(result["spring"].receipts)
    ) <= 12
    assert len(result["spring"].receipts) == 1  # Discarded search evidence is never published.
    if changed:
        assert actual["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert actual["sources"] == []
    else:
        assert actual["outcome"] == "ANSWER"
        assert actual["sources"] == [{"tool": "CHARACTER_EVIDENCE", "targetId": uid(801)}]
        assert not actual.get("retrievalSources", [])
        assert NOTICES["PARTIAL_ANSWER"] not in actual["limitations"]
        assert "관련 자료를 모두 검토한 결과는 아니에요" not in actual["text"]
        assert "확인하지 못" in actual["text"]


@pytest.mark.parametrize("model", [HybridPlan, GroundedAnswer, FieldSelection])
def test_new_planner_and_coverage_fields_are_required_in_closed_provider_schema(model):
    def check(node):
        if isinstance(node, dict):
            if node.get("type") == "object":
                assert node["additionalProperties"] is False
                assert set(node["required"]) == set(node["properties"])
            assert "default" not in node
            for value in node.values():
                check(value)
        elif isinstance(node, list):
            for value in node:
                check(value)

    check(response_schema(model).schema)
