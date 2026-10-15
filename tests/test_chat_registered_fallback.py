"""Offline boundary checks for supplementing missing registered information.

Scripted model decisions exercise the real handler and execution ledger. They
are synthetic contract regressions, not a measurement of provider accuracy.
"""

import asyncio
import copy
import socket
from uuid import UUID

import pytest

from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay, scenario
from tests.test_chat_hybrid_items import passage
from tests.test_chat_requested_parts import answer, assert_answer, claim, search_with


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Registered fallback replays cannot access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


SOURCE = "미라는 세린을 오랫동안 함께 지낸 친구라고 소개했다."


def registered_case(*, fallback=True, attribute=None):
    case = scenario()
    case.update(question="미라의 친구는 누구야?", keyword=[], vector=[])
    case["outputs"] = [
        {
            "disposition": "PROCEED",
            "rdbMode": "CURRENT",
            "sourceNeed": "NONE",
            "subjects": [
                {
                    "domain": "CHARACTER",
                    "name": "미라",
                    "focus": {"factType": "PROFILE", "attribute": attribute},
                }
            ],
            "searchTerms": ["미라", "친구"],
            "includeEvidence": False,
            "episodeFrom": None,
            "episodeTo": None,
            "characterFilter": None,
            "perItem": False,
            "requestedFacets": [],
            "requestedParts": [{"ref": "P1", "description": "미라의 친구"}] if fallback else [],
            "eventContext": None,
            "eventScope": None,
            "includeRegisteredValues": False,
            "sourceFallback": fallback,
        }
    ]
    return case


def source_answer():
    return answer(claim(SOURCE, SOURCE, parts=("P1",), event_match=None))


def catalog(*rows, registered=True):
    def setup(spring):
        spring.catalog = (
            [catalog_subject(701, "미라", current=copy.deepcopy(list(rows)))] if registered else []
        )

    return setup


def tools(result):
    return [query["tool"] for query in result["spring"].queries]


def assert_no_search(result):
    assert result["search"].calls == []
    assert result["embedding"].inputs == []
    assert result["spring"].receipts == []


@pytest.mark.parametrize("missing", ["subject", "fields", "values"])
def test_complete_registered_gap_uses_scoped_keyword_search_once(tmp_path, missing):
    case = registered_case(attribute="친구" if missing == "fields" else None)
    case.update(episodeFrom=3, episodeTo=5)
    case["outputs"].append(source_answer())

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=catalog(registered=missing != "subject"),
            search_setup=search_with(keyword=[passage(SOURCE, 1, episode=4)]),
        )
    )

    response = assert_answer(result)
    assert SOURCE in response["text"]
    assert result["search"].calls.count("keyword") == 1
    assert "vector" not in result["search"].calls
    assert tools(result).count("FIND_CHARACTERS") == 1
    assert len(result["raw"].requests) == 2
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2
    assert result["embedding"].inputs == []
    payload = result["raw"].requests[-1]["input"]
    assert payload["plan"]["sourceNeed"] == "SCENE"
    assert payload["plan"]["sourceFallback"] is False
    assert payload["requestedFacets"] == ["P1"]
    assert payload["rows"] == []
    for scope in result["search"].scopes:
        assert scope.member_id == 42
        assert scope.work_id == UUID(int=9003)
        assert scope.episode_from == 3 and scope.episode_to == 5


def test_keyword_miss_can_use_one_metered_vector_embedding(tmp_path):
    case = registered_case()
    case["outputs"].append(source_answer())
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=catalog(),
            search_setup=search_with(vector=[passage(SOURCE, 1, method="VECTOR")]),
        )
    )

    assert_answer(result)
    assert result["search"].calls.count("keyword") == 1
    assert result["search"].calls.count("vector") == 1
    assert len(result["embedding"].inputs) == 1
    assert len(result["raw"].requests) == 2
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 3
    assert tools(result) == ["FIND_CHARACTERS", "CHARACTER_CURRENT"]


def test_registered_only_question_does_not_supplement_an_empty_result(tmp_path):
    case = registered_case(fallback=False)
    case["question"] = "등록 정보에 있는 미라의 친구만 알려줘."
    result = asyncio.run(replay(tmp_path, case, spring_setup=catalog()))

    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert len(result["raw"].requests) == 1
    assert_no_search(result)


def test_explicit_registered_negative_is_an_answer_not_a_missing_value(tmp_path):
    row = item(701, "미라", category="PROFILE", key="friend", label="친구", value="없음")
    result = asyncio.run(replay(tmp_path, registered_case(), spring_setup=catalog(row)))

    response = assert_answer(result)
    assert "없음" in response["text"]
    assert len(result["raw"].requests) == 1
    assert_no_search(result)


def test_collection_missing_values_cannot_be_promoted_to_narrative_matches(tmp_path):
    case = registered_case()
    case["outputs"][0].update(
        subjects=[],
        collection={
            "domain": "CHARACTER",
            "factType": "PROFILE",
            "worldCategory": None,
            "attribute": None,
            "predicate": "친구가 있는",
        },
        answerShape="LIST",
    )
    case["question"] = "친구가 있는 인물은 누구야?"
    case["outputs"][0]["searchTerms"] = ["친구"]
    result = asyncio.run(replay(tmp_path, case, spring_setup=catalog()))

    assert result["finish"]["failure"] is None
    assert "CHARACTER_CURRENT_COLLECTION" in tools(result)
    assert_no_search(result)


@pytest.mark.parametrize("partial_tool", ["FIND_CHARACTERS", "CHARACTER_CURRENT"])
def test_partial_lookup_is_not_an_empty_registered_fact(tmp_path, partial_tool):
    def setup(spring):
        catalog()(spring)
        lookup = spring.lookup

        def partial(query):
            response = lookup(query)
            if query.tool.value == partial_tool:
                response["partial"] = True
            return response

        spring.lookup = partial

    result = asyncio.run(replay(tmp_path, registered_case(), spring_setup=setup))

    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] in {
        "CLARIFICATION",
        "INSUFFICIENT_EVIDENCE",
    }
    assert_no_search(result)


@pytest.mark.parametrize("status", [403, 503])
def test_lookup_failure_never_becomes_a_manuscript_fallback(tmp_path, status):
    def setup(spring):
        catalog()(spring)
        spring.query_status = status

    result = asyncio.run(replay(tmp_path, registered_case(), spring_setup=setup))

    assert result["finish"]["failure"] is not None
    assert len(result["raw"].requests) == 1
    assert_no_search(result)


def test_rows_rejected_by_registered_selection_do_not_reappear_in_source_answer(tmp_path):
    case = registered_case()
    # Recorded pre-focus plans still use the row selector. Its rejected rows must
    # not become source context merely because they remain in the lookup cache.
    del case["outputs"][0]["subjects"][0]["focus"]
    case["outputs"].extend([{"outcome": "INSUFFICIENT_EVIDENCE", "rowRefs": []}, source_answer()])
    unrelated = item(
        701, "미라", category="PROFILE", key="occupation", label="직업", value="대장장이"
    )
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=catalog(unrelated),
            search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        )
    )

    response = assert_answer(result)
    assert result["raw"].requests[1]["input"]["rows"]
    assert result["raw"].requests[-1]["input"]["rows"] == []
    assert "대장장이" not in response["text"]
    assert tools(result) == ["FIND_CHARACTERS", "CHARACTER_CURRENT"]
    assert len(result["raw"].requests) == 3


def test_unrelated_observed_fields_can_fall_back_without_reading_their_values(tmp_path):
    case = registered_case(attribute="친구")
    case["maxCalls"] = 3
    case["outputs"].extend([{"outcome": "INSUFFICIENT_EVIDENCE", "fieldRefs": []}, source_answer()])
    row = item(701, "미라", category="PROFILE", key="occupation", label="직업", value="대장장이")
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_model_calls": 3},
            spring_setup=catalog(row),
            search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        )
    )

    assert_answer(result)
    assert tools(result) == ["FIND_CHARACTERS", "CHARACTER_FIELDS"]
    assert result["raw"].requests[-1]["input"]["rows"] == []
    assert len(result["raw"].requests) == 3
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 3


def test_registered_synthesis_gap_does_not_hide_a_concurrent_change(tmp_path):
    case = registered_case()
    case["outputs"][0]["answerShape"] = "SUMMARY"
    case["outputs"].append({"claims": []})
    row = item(701, "미라", category="PROFILE", key="occupation", label="직업", value="대장장이")

    def change_after_synthesis(call, spring, _search):
        if call == 2:
            spring.catalog[0]["current"][0]["settingVersion"] += 1

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=catalog(row),
            model_hook=change_after_synthesis,
        )
    )

    assert result["finish"]["failure"] is None
    response = result["finish"]["result"]
    assert response["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert "현재 설정이 변경" in response["text"]
    assert tools(result).count("CHARACTER_CURRENT") == 2
    assert len(result["raw"].requests) == 2
    assert_no_search(result)


@pytest.mark.parametrize("shape", ["COUNT", "DISTINCT"])
def test_registered_aggregate_does_not_count_manuscript_mentions(tmp_path, shape):
    case = registered_case()
    case["outputs"][0]["answerShape"] = shape
    result = asyncio.run(replay(tmp_path, case, spring_setup=catalog()))

    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert_no_search(result)


@pytest.mark.parametrize("budget", ["model", "execution", "tools"])
def test_fallback_keeps_registered_result_when_shared_budget_is_exhausted(tmp_path, budget):
    case = registered_case()
    hybrid = {"max_model_calls": 1} if budget == "model" else None
    if budget == "execution":
        case["maxCalls"] = 1
    if budget == "tools":
        case["maxToolCalls"] = 5
    result = asyncio.run(replay(tmp_path, case, spring_setup=catalog(), hybrid=hybrid))

    assert result["finish"]["failure"] is None
    assert result["finish"]["result"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert len(result["raw"].requests) == 1
    assert_no_search(result)


def test_fallback_uses_the_last_text_call_without_replanning(tmp_path):
    case = registered_case()
    case["maxCalls"] = 2
    case["outputs"].append(source_answer())
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            hybrid={"max_model_calls": 2},
            spring_setup=catalog(),
            search_setup=search_with(keyword=[passage(SOURCE, 1)]),
        )
    )

    assert_answer(result)
    assert len(result["raw"].requests) == 2
    assert len(result["spring"].reservations) == len(result["spring"].reports) == 2
    assert result["embedding"].inputs == []
