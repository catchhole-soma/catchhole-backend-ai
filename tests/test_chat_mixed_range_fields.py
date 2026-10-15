"""Scoped mixed lookup regressions with authored outputs and no provider/network calls."""

import asyncio
from dataclasses import replace
from hashlib import sha256
import socket

import pytest

from app.chatbot.qa_render import NOTICES
from tests.test_chat_focused_lookup import catalog_subject, item
from tests.test_chat_hybrid import replay, scenario, source_chunk


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Mixed range tests cannot use external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def catalog(domain):
    name = "세린" if domain == "CHARACTER" else "술법"
    category = "STAT" if domain == "CHARACTER" else "POWER_SYSTEM"

    def row(path, value, *, episode=None, number=1001):
        return item(
            701,
            name,
            kind="HISTORY" if episode else "CURRENT",
            category=category,
            key=f"mana.{path}" if domain == "CHARACTER" else "mana",
            scope=None if domain == "CHARACTER" or path == "a_current" else path,
            label="소모량",
            value=value,
            episode=episode,
            number=number,
        )

    return [
        catalog_subject(
            701,
            name,
            domain=domain,
            current=[row("a_current", "90"), row("b_shared", "30")],
            history=[
                row("b_shared", "20", episode=15),
                row("c_history", "10", episode=12, number=1002),
                row("d_outside", "5", episode=5, number=1003),
            ],
        )
    ]


def field_case(domain="CHARACTER", *, mode="CURRENT_AND_HISTORY", ranged=True):
    spec = scenario()
    name = "세린" if domain == "CHARACTER" else "술법"
    spec["question"] = f"{name}의 현재 마력과 10~20화의 마력 변화는?"
    decision = spec["outputs"][0]
    decision.update(
        rdbMode=mode,
        sourceNeed="NONE",
        searchTerms=[],
        includeEvidence=False,
        subjects=[
            {
                "domain": domain,
                "name": name,
                "focus": {
                    "factType": "STAT" if domain == "CHARACTER" else None,
                    "attribute": "마력",
                },
            }
        ],
        episodeFrom=10 if ranged else None,
        episodeTo=20 if ranged else None,
    )
    count = (
        2 if mode == "HISTORY" and ranged else 3 if mode == "CURRENT_AND_HISTORY" and ranged else 4
    )
    spec["outputs"] = [
        decision,
        {"outcome": "ANSWER", "fieldRefs": [f"F{n}" for n in range(1, count + 1)]},
    ]
    return spec


def setup_catalog(domain):
    def setup(spring):
        spring.catalog = catalog(domain)

    return setup


def queries(result, tool):
    return [q for q in result["spring"].queries if q["tool"] == tool]


def response(result):
    assert result["finish"]["failure"] is None, result["finish"]
    return result["finish"]["result"]


def field_paths(result):
    return {
        (f["category"], f["scopeName"], f["settingKey"])
        for f in result["raw"].requests[1]["input"]["fields"]
    }


def row_paths(rows):
    return {(r["category"], r["scopeName"], r["settingKey"]) for r in rows}


@pytest.mark.parametrize("domain", ["CHARACTER", "WORLD"])
def test_mixed_range_selects_current_and_bounded_history_paths_and_reuses_current(tmp_path, domain):
    spec = field_case(domain)
    spec["maxToolCalls"] = 7
    result = asyncio.run(replay(tmp_path, spec, spring_setup=setup_catalog(domain)))
    actual = response(result)
    source = catalog(domain)[0]
    expected = row_paths(source["current"] + source["history"][:2])
    assert field_paths(result) == expected and len(expected) == 3
    assert all(
        "value" not in field and "subjectId" not in field
        for field in result["raw"].requests[1]["input"]["fields"]
    )
    assert actual["outcome"] == "ANSWER"
    assert all(value in actual["text"] for value in ("90", "30", "20", "10"))
    assert NOTICES["PARTIAL_RESULTS"] not in actual["limitations"]
    field_queries = queries(result, f"{domain}_FIELDS")
    assert len(field_queries) == 1
    assert (field_queries[0]["episodeFrom"], field_queries[0]["episodeTo"]) == (10, 20)
    current = queries(result, f"{domain}_CURRENT")
    assert len(current) == 2  # One cache fill, one post-FIELD version check, no path re-fetch.
    assert all(
        q["settingKey"] is None and q["episodeFrom"] is None and q["episodeTo"] is None
        for q in current
    )
    assert current[-1]["size"] == 1
    history = queries(result, f"{domain}_HISTORY")
    assert len(history) == 3
    assert all((q["episodeFrom"], q["episodeTo"]) == (10, 20) for q in history)
    assert not any(
        q["scopeName"] == "d_outside" or q["settingKey"] == "mana.d_outside" for q in history
    )
    assert len(result["spring"].queries) == 7
    assert len(result["raw"].requests) == 2


@pytest.mark.parametrize("domain", ["CHARACTER", "WORLD"])
@pytest.mark.parametrize(
    "mode,ranged", [("CURRENT", True), ("HISTORY", True), ("CURRENT_AND_HISTORY", False)]
)
def test_other_lookup_modes_keep_existing_field_contract(tmp_path, domain, mode, ranged):
    result = asyncio.run(
        replay(
            tmp_path,
            field_case(domain, mode=mode, ranged=ranged),
            spring_setup=setup_catalog(domain),
        )
    )
    assert response(result)["outcome"] == "ANSWER"
    source = catalog(domain)[0]
    bounded_history_only = mode == "HISTORY" and ranged
    expected = row_paths(
        source["history"][:2] if bounded_history_only else source["current"] + source["history"]
    )
    assert field_paths(result) == expected
    fields = queries(result, f"{domain}_FIELDS")
    assert len(fields) == 1
    assert (fields[0]["episodeFrom"], fields[0]["episodeTo"]) == (
        (10, 20) if bounded_history_only else (None, None)
    )
    current = queries(result, f"{domain}_CURRENT")
    if mode == "HISTORY":
        assert not current
    else:
        assert all(q["settingKey"] is not None for q in current)


@pytest.mark.parametrize("domain", ["CHARACTER", "WORLD"])
@pytest.mark.parametrize("mutation", ["version", "name", "deleted"])
def test_field_selection_cannot_publish_current_values_changed_after_prefetch(
    tmp_path, domain, mutation
):
    def after_selection(count, spring, search):
        if count != 2:
            return
        if mutation == "deleted":
            spring.catalog.clear()
        else:
            for row in spring.catalog[0]["current"]:
                row.update(
                    {"value": "99", "settingVersion": 2}
                    if mutation == "version"
                    else {"subjectName": "바뀐 이름"}
                )

    result = asyncio.run(
        replay(
            tmp_path,
            field_case(domain),
            spring_setup=setup_catalog(domain),
            model_hook=after_selection,
        )
    )
    actual = response(result)
    assert actual["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert NOTICES["CURRENT_CHANGED"] in actual["limitations"]
    assert "90" not in actual["text"] and "30" not in actual["text"]
    assert len(queries(result, f"{domain}_CURRENT")) == 2


@pytest.mark.parametrize("incomplete", ["current", "history_fields"])
def test_incomplete_field_union_stops_before_selection(tmp_path, incomplete):
    spec = field_case()
    spec["maxToolCalls"] = 6

    def setup(spring):
        source = catalog("CHARACTER")[0]
        if incomplete == "current":
            source["current"].append(dict(source["current"][0], settingKey="mana.extra"))
        else:
            source["current"] = source["current"][:1]
            source["history"].append(dict(source["history"][0], settingKey="mana.extra"))
        spring.catalog = [source]

    result = asyncio.run(replay(tmp_path, spec, spring_setup=setup, config={"page_size": 1}))
    actual = response(result)
    assert actual["outcome"] == "CLARIFICATION"
    assert NOTICES["PARTIAL_RESULTS"] in actual["limitations"]
    assert len(result["raw"].requests) == 1
    assert not queries(result, "CHARACTER_HISTORY")
    assert len(result["spring"].queries) < 6


def test_history_pagination_cannot_spend_the_cached_current_check_budget(tmp_path):
    spec = field_case()
    spec["maxToolCalls"] = 6
    result = asyncio.run(replay(tmp_path, spec, spring_setup=setup_catalog("CHARACTER")))
    actual = response(result)
    assert actual["outcome"] == "ANSWER"
    assert NOTICES["PARTIAL_RESULTS"] in actual["limitations"]
    assert len(result["spring"].queries) == 6
    assert result["spring"].queries[-1]["tool"] == "CHARACTER_CURRENT"
    assert result["spring"].queries[-1]["size"] == 1
    assert len(queries(result, "CHARACTER_HISTORY")) == 2


@pytest.mark.parametrize("changed", [False, True])
@pytest.mark.parametrize("source_needed", [False, True])
def test_later_synthesis_keeps_its_own_final_current_check(tmp_path, changed, source_needed):
    spec = field_case()
    decision = spec["outputs"][0]
    if source_needed:
        spec["maxToolCalls"] = 12
        decision.update(
            sourceNeed="REASON",
            searchTerms=["마력"],
            requestedFacets=["WHY"],
            includeRegisteredValues=True,
        )
        spec["outputs"].append(
            {
                "coverage": "COMPLETE",
                "claims": [
                    {
                        "text": "훈련으로 마력이 늘었습니다.",
                        "evidenceRefs": ["E1"],
                        "rowRefs": ["R1"],
                        "answeredFacets": ["WHY"],
                    }
                ],
                "conflicts": [],
            }
        )
    else:
        spec["maxToolCalls"] = 8
        decision["answerShape"] = "SUMMARY"
        spec["outputs"].append({"claims": [{"text": "현재 마력은 90입니다.", "rowRefs": ["R1"]}]})

    def setup_source(search):
        text = "세린은 훈련으로 마력이 늘었다."
        search.keyword_chunks = [
            replace(
                source_chunk("ring"),
                text=text,
                episode_no=15,
                end_offset=len(text),
                chunk_hash=sha256(text.encode()).hexdigest(),
            )
        ]

    def after_synthesis(count, spring, search):
        if changed and count == 3:
            for row in spring.catalog[0]["current"]:
                row.update(value="99", settingVersion=2)

    result = asyncio.run(
        replay(
            tmp_path,
            spec,
            spring_setup=setup_catalog("CHARACTER"),
            search_setup=setup_source,
            model_hook=after_synthesis,
            hybrid={"max_rounds": 1},
        )
    )
    actual = response(result)
    assert len(queries(result, "CHARACTER_CURRENT")) == 3
    assert len(result["raw"].requests) == 3
    assert (
        len(result["spring"].queries) + len(result["search"].calls) + len(result["spring"].receipts)
        <= spec["maxToolCalls"]
    )
    if changed:
        assert actual["outcome"] == "INSUFFICIENT_EVIDENCE"
        assert NOTICES["CURRENT_CHANGED"] in actual["limitations"]
        assert not actual["sources"] and not actual.get("retrievalSources")
    else:
        assert actual["outcome"] == "ANSWER"
