"""Synthetic concurrent-edit regressions at the real hybrid/HTTP/outbox boundaries.

Model outputs are authored fixtures, not live-provider or semantic quality scores.
"""

import asyncio
import copy
from dataclasses import replace
from hashlib import sha256
import json
import socket

import httpx
import pytest

from app.chatbot.qa_render import NOTICES
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay, scenario, source_chunk


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Current revalidation tests cannot use external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def named_case(domain="CHARACTER", *, mode="CURRENT", per_item=False):
    case = scenario()
    name = "세린" if domain == "CHARACTER" else "화염술"
    case["question"] = f"{name}의 현재 등급과 그 등급이 된 이유는?"
    plan = case["outputs"][0]
    plan.update(
        rdbMode=mode,
        sourceNeed="REASON",
        subjects=[{"domain": domain, "name": name}],
        searchTerms=[name, "등급"],
        requestedFacets=["WHY"],
        perItem=per_item,
        includeRegisteredValues=True,
    )
    refs = ["R1", "R2"] if mode == "CURRENT_AND_HISTORY" else ["R1"]
    text = f"{name}의 등급은 7이고, 시험을 통과해 얻었습니다."
    grounded = {"coverage": "COMPLETE", "claims": [], "conflicts": []}
    if per_item:
        grounded["itemAnswers"] = [
            {
                "itemRef": "I1",
                "answers": [{"facet": "WHY", "text": text, "evidenceRefs": ["E1"]}],
                "missingFacets": [],
            }
        ]
    else:
        grounded["claims"] = [
            {
                "text": text,
                "evidenceRefs": ["E1"],
                "rowRefs": refs,
                "answeredFacets": ["WHY"],
            }
        ]
    case["outputs"] = [plan, {"outcome": "ANSWER", "rowRefs": refs}, grounded]
    return case


def named_catalog(domain="CHARACTER"):
    name = "세린" if domain == "CHARACTER" else "화염술"
    category = "LEVEL" if domain == "CHARACTER" else "POWER_SYSTEM"
    current = item(701, name, category=category, key="rank", label="등급", value="7")
    history = dict(current, kind="HISTORY", id="00000000-0000-0000-0000-000000000901")
    history.update(operation="ADD", contributesToCurrent=False, sourceEpisodeNo=3)
    return [catalog_subject(701, name, domain=domain, current=[current], history=[history])]


def setup_named(domain):
    def setup(spring):
        spring.catalog = named_catalog(domain)

    return setup


def setup_source(search):
    text = "세린과 화염술은 시험을 통과해 등급 7을 얻었다."
    search.keyword_chunks = [
        replace(
            source_chunk("ring"),
            text=text,
            end_offset=len(text),
            chunk_hash=sha256(text.encode()).hexdigest(),
        )
    ]


def mutate_after_answer(case, mutation):
    def hook(count, spring, search):
        if count != len(case["outputs"]):
            return
        if mutation == "version":
            spring.catalog[0]["current"][0].update(value="9", settingVersion=2)
        elif mutation == "name":
            spring.catalog[0]["subject"]["subjectName"] = "변경된 이름"
            spring.catalog[0]["current"][0]["subjectName"] = "변경된 이름"
        elif mutation == "deleted":
            spring.catalog.clear()
        elif mutation == "empty":
            spring.catalog[0]["current"].clear()
        elif mutation == "http404":
            spring.final_current_missing = True

    return hook


def current_queries(result, domain):
    return [q for q in result["spring"].queries if q["tool"] == f"{domain}_CURRENT"]


def assert_changed(result):
    finish = result["finish"]
    assert finish["failure"] is None, finish
    response = finish["result"]
    assert response["outcome"] == "INSUFFICIENT_EVIDENCE", response
    assert NOTICES["CURRENT_CHANGED"] in response["limitations"]
    assert "7" not in response["text"]
    assert not response["sources"] and not response.get("retrievalSources")
    assert "설정" in response["text"] and "변경" in response["text"]


@pytest.mark.parametrize("domain", ["CHARACTER", "WORLD"])
@pytest.mark.parametrize("mode", ["CURRENT", "CURRENT_AND_HISTORY"])
@pytest.mark.parametrize("per_item", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_named_current_is_rechecked_after_narrative_generation(
    tmp_path, domain, mode, per_item, changed
):
    case = named_case(domain, mode=mode, per_item=per_item)
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_named(domain),
            search_setup=setup_source,
            model_hook=mutate_after_answer(case, "version" if changed else None),
            hybrid={"max_rounds": 1},
        )
    )
    queries = current_queries(result, domain)
    assert len(queries) == 2, queries
    assert queries[-1]["targetId"] == queries[0]["targetId"]
    assert queries[-1]["size"] == 1
    if changed:
        assert_changed(result)
    else:
        assert result["finish"]["failure"] is None, result["finish"]
        assert result["finish"]["result"]["outcome"] == "ANSWER"
        assert "7" in result["finish"]["result"]["text"]
        assert NOTICES["CURRENT_CHANGED"] not in result["finish"]["result"]["limitations"]


@pytest.mark.parametrize("domain", ["CHARACTER", "WORLD"])
@pytest.mark.parametrize("mutation", ["name", "deleted", "empty", "http404"])
def test_current_identity_or_existence_change_never_publishes_old_answer(
    tmp_path, domain, mutation
):
    case = named_case(domain)

    def setup(spring):
        setup_named(domain)(spring)
        original = spring.respond
        spring.final_current_missing = False

        def respond(request):
            if spring.final_current_missing and request.url.path.endswith("/tools"):
                body = json.loads(request.content)
                if body["tool"] == f"{domain}_CURRENT":
                    spring.queries.append(body)
                    return httpx.Response(404, json={"success": False})
            return original(request)

        spring.respond = respond

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup,
            search_setup=setup_source,
            model_hook=mutate_after_answer(case, mutation),
            hybrid={"max_rounds": 1},
        )
    )
    assert len(current_queries(result, domain)) == 2
    assert_changed(result)


@pytest.mark.parametrize("domain", ["CHARACTER", "WORLD"])
def test_history_answer_does_not_depend_on_live_current_version(tmp_path, domain):
    case = named_case(domain, mode="HISTORY")
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_named(domain),
            search_setup=setup_source,
            model_hook=mutate_after_answer(case, "version"),
            hybrid={"max_rounds": 1},
        )
    )
    assert result["finish"]["failure"] is None, result["finish"]
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert not current_queries(result, domain)


def test_source_only_answer_does_not_add_current_queries(tmp_path):
    result = asyncio.run(replay(tmp_path, scenario(), hybrid={"max_rounds": 1}))
    assert result["finish"]["failure"] is None, result["finish"]
    assert result["finish"]["result"]["outcome"] == "ANSWER"
    assert not result["spring"].queries


@pytest.mark.parametrize("per_item", [False, True])
@pytest.mark.parametrize("changed", [False, True])
def test_search_leaves_budget_for_final_current_recheck(tmp_path, per_item, changed):
    case = named_case(per_item=per_item)
    case["maxToolCalls"] = 7
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_named("CHARACTER"),
            search_setup=setup_source,
            model_hook=mutate_after_answer(case, "version" if changed else None),
            hybrid={"max_rounds": 1},
        )
    )
    assert result["finish"]["failure"] is None, result["finish"]
    assert len(current_queries(result, "CHARACTER")) == 2
    tool_count = (
        len(result["spring"].queries) + len(result["search"].calls) + len(result["spring"].receipts)
    )
    assert tool_count <= case["maxToolCalls"]
    if changed:
        assert_changed(result)
    else:
        assert result["finish"]["result"]["outcome"] == "ANSWER"
        assert len(result["spring"].receipts) == 2
        assert result["search"].calls[-1] == "revalidate"


def test_item_none_does_not_repeat_current_items_deleted_during_answer(tmp_path):
    case = named_case(per_item=True)
    case["outputs"][-1] = {
        "coverage": "NONE",
        "claims": [],
        "conflicts": [],
        "itemAnswers": [{"itemRef": "I1", "answers": [], "missingFacets": ["WHY"]}],
    }
    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup_named("CHARACTER"),
            search_setup=setup_source,
            model_hook=mutate_after_answer(case, "deleted"),
            hybrid={"max_rounds": 1},
        )
    )
    assert len(current_queries(result, "CHARACTER")) == 2
    assert_changed(result)
    assert "세린" not in result["finish"]["result"]["text"]


def collection_case(domain="CHARACTER", *, per_item=False):
    case = named_case(domain, per_item=per_item)
    case["question"] = "등록된 등급들과 각각의 이유는?"
    plan = case["outputs"][0]
    plan.update(
        subjects=[],
        searchTerms=["등급"],
        collection={
            "domain": domain,
            "factType": "LEVEL" if domain == "CHARACTER" else None,
            "worldCategory": "POWER_SYSTEM" if domain == "WORLD" else None,
            "attribute": None,
            "predicate": None,
        },
    )
    # Collections preserve focused rows without a separate model selection.
    del case["outputs"][1]
    return case


@pytest.mark.parametrize("domain", ["CHARACTER", "WORLD"])
@pytest.mark.parametrize("per_item", [False, True])
@pytest.mark.parametrize("status", [409, 429, 500])
def test_collection_final_http_conflict_is_change_guidance_not_network_failure(
    tmp_path, domain, per_item, status
):
    case = collection_case(domain, per_item=per_item)

    def setup(spring):
        setup_named(domain)(spring)
        original = spring.respond

        def respond(request):
            if request.url.path.endswith("/tools"):
                body = json.loads(request.content)
                if body["tool"].endswith("_COLLECTION") and body.get("readToken"):
                    spring.queries.append(body)
                    return httpx.Response(
                        status,
                        json={"success": False, "error": {"code": "CONCURRENT_UPDATE"}},
                    )
            return original(request)

        spring.respond = respond

    result = asyncio.run(
        replay(
            tmp_path,
            case,
            spring_setup=setup,
            search_setup=setup_source,
            hybrid={"max_rounds": 1},
        )
    )
    queries = result["spring"].queries
    assert len(queries) == 2 and queries[-1]["readToken"]
    assert len(result["raw"].requests) == 2
    if status == 409:
        assert_changed(result)
    else:
        assert result["finish"]["result"] is None
        expected = "EXECUTION_LIMIT_EXCEEDED" if status == 429 else "NETWORK_FAILURE"
        assert result["finish"]["failure"] == expected


@pytest.mark.parametrize("domain", ["CHARACTER", "WORLD"])
def test_collection_internal_snapshot_mismatch_is_change_guidance(tmp_path, domain):
    case = collection_case(domain)

    def setup(spring):
        setup_named(domain)(spring)
        second = copy.deepcopy(spring.catalog[0]["current"][0])
        second.update(settingKey="rank-other", settingVersion=2)
        spring.catalog[0]["current"].append(second)

    result = asyncio.run(replay(tmp_path, case, spring_setup=setup))
    assert len(result["raw"].requests) == 1  # Stop before presenting inconsistent rows to a model.
    assert_changed(result)
    assert not result["search"].calls
