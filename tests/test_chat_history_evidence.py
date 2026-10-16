"""Offline history/direct-source regressions, not planner accuracy measurements.

Scripted plans exercise the production hybrid handler, bounded Spring queries,
reference validation and outbox. No provider, manuscript, or network is used.
The direct route returns verified source references; Spring owns the displayed
quotation, so these tests do not pretend the Python result contains that text.
"""

import asyncio
import copy
import json
import socket

import pytest

from app.chatbot.qa_render import NOTICES
from tests.test_chat_focused_lookup import catalog_subject, item, uid
from tests.test_chat_hybrid import replay, scenario


SUBJECT, NAME = 710, "하린"
KEY, LABEL = "stats.resonance", "공명력"


@pytest.fixture(autouse=True)
def offline_only(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("History regressions must not access external services")

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(socket.socket, "connect_ex", reject)


def history_case(*, mode="HISTORY", episode_from=None, episode_to=None):
    case = scenario()
    case.update(
        question=f"{NAME}의 {LABEL} 변경 이력 전부와 각 기록의 원문을 보여줘",
        keyword=[],
        vector=[],
    )
    if episode_from is not None and episode_to is not None:
        case["question"] += f". {episode_from}~{episode_to}화에서만."
    case["outputs"] = [
        {
            "disposition": "PROCEED",
            "rdbMode": mode,
            "sourceNeed": "NONE",
            "subjects": [
                {
                    "domain": "CHARACTER",
                    "name": NAME,
                    "focus": {"factType": "STAT", "attribute": LABEL},
                }
            ],
            "searchTerms": [],
            "includeEvidence": True,
            "includeRegisteredValues": True,
            "episodeFrom": episode_from,
            "episodeTo": episode_to,
            "characterFilter": None,
            "perItem": False,
            "requestedFacets": [],
            "answerShape": "FACTS",
        }
    ]
    return case


def source(number, episode, value, *, status="AVAILABLE"):
    evidence = item(
        SUBJECT,
        NAME,
        kind="EVIDENCE",
        category="STAT",
        key=KEY,
        label=LABEL,
        value=value,
        episode=episode,
    )
    evidence["id"] = uid(number)
    quote = f"[합성 원문 {episode}] {NAME}의 {LABEL} 수치: {value}."
    evidence["evidence"] = {
        "status": status,
        "candidateId": uid(number),
        "episodeId": uid(10000 + episode),
        "sourceEpisodeNo": episode,
        "sourceChunkId": uid(20000 + episode),
        "sourceHash": "a" * 64,
        "sourceVersion": "synthetic-v1",
        "matchesCurrentSource": status == "AVAILABLE",
        "quotes": [{"text": quote, "startOffset": 0, "endOffset": len(quote)}]
        if status == "AVAILABLE"
        else [],
        "truncated": False,
    }
    return evidence


def catalog(values=("12", "27", "27", "41", "53"), *, episodes=None):
    episodes = episodes or list(range(1, len(values) + 1))
    history, evidence = [], []
    for index, (episode, value) in enumerate(zip(episodes, values, strict=True), 1):
        ref = {"tool": "CHARACTER_EVIDENCE", "targetId": uid(30000 + index)}
        row = item(
            SUBJECT,
            NAME,
            kind="HISTORY",
            category="STAT",
            key=KEY,
            label=LABEL,
            value=value,
            episode=episode,
            number=40000 + index,
        )
        row["sources"] = [ref]
        row["operation"] = "ADD" if index == 1 else "UPDATE"
        history.append(row)
        evidence.append(source(30000 + index, episode, value))
    current = item(SUBJECT, NAME, category="STAT", key=KEY, label=LABEL, value="89")
    current["origin"] = "USER_INPUT"
    subject = catalog_subject(SUBJECT, NAME, current=[current], history=history)
    subject["evidence"] = evidence
    return [subject]


def run_case(tmp_path, case, subjects, **kwargs):
    def setup(spring):
        spring.catalog = copy.deepcopy(subjects)

    return asyncio.run(replay(tmp_path, case, spring_setup=setup, **kwargs))


def finished(result):
    assert result["finish"]["failure"] is None, result["finish"]
    return result["finish"]["result"]


def queries(result, tool):
    return [q for q in result["spring"].queries if q["tool"] == tool]


def test_history_and_each_source_survive_multiple_pages_without_answer_generation(tmp_path):
    subjects = catalog()
    result = run_case(
        tmp_path,
        history_case(),
        subjects,
        config={"page_size": 2, "max_pages": 1, "max_selected_rows": 1},
    )
    answer = finished(result)
    assert answer["outcome"] == "ANSWER"
    assert [q["page"] for q in queries(result, "CHARACTER_HISTORY")] == [0, 1, 2]
    assert all(q["settingKey"] == KEY for q in queries(result, "CHARACTER_HISTORY"))
    assert answer["sources"] == [row["sources"][0] for row in subjects[0]["history"]]
    for episode, value in enumerate(["12", "27", "27", "41", "53"], 1):
        assert f"{LABEL}: {value} (출처 {episode}화) · 근거 {episode}" in answer["text"]
    assert "89" not in answer["text"]
    assert len(result["raw"].requests) == 1  # Authored planner only; no answer/row selector.
    assert result["search"].calls == result["embedding"].inputs == []
    assert [r["purpose"] for r in result["spring"].reservations] == ["CHAT_INTERPRETATION"]
    assert NOTICES["PARTIAL_RESULTS"] not in answer["limitations"]
    assert NOTICES["EVIDENCE_LIMIT"] not in answer["limitations"]
    assert "원문 미확인" not in answer["text"]
    assert NOTICES["HISTORY_IS_NOT_A_STATE_SNAPSHOT"] in answer["limitations"]
    # Source text is owned by the validated Spring reference, never rewritten by the model.
    model_inputs = json.dumps([r["input"] for r in result["raw"].requests], ensure_ascii=False)
    assert "합성 원문" not in model_inputs


@pytest.mark.parametrize(
    "status",
    ["SOURCE_MISSING", "SOURCE_PURGED", "SOURCE_CHANGED", "STORAGE_UNAVAILABLE"],
)
def test_unavailable_one_source_keeps_every_history_record_and_other_sources(tmp_path, status):
    subjects = catalog()
    subjects[0]["evidence"][2] = source(30003, 3, "27", status=status)
    result = run_case(tmp_path, history_case(), subjects)
    answer = finished(result)
    assert answer["outcome"] == "ANSWER"
    assert len(answer["sources"]) == 4
    assert uid(30003) not in {r["targetId"] for r in answer["sources"]}
    assert f"{LABEL}: 27 (출처 3화)" in answer["text"]
    assert f"{LABEL}: 27 (출처 3화) · 원문 미확인" in answer["text"]
    assert answer["text"].count("원문 미확인") == 1
    assert all(f"(출처 {episode}화)" in answer["text"] for episode in range(1, 6))
    assert NOTICES[status] in answer["limitations"]
    assert len(result["raw"].requests) == 1


def test_missing_source_reference_is_not_a_missing_history_or_provider_error(tmp_path):
    subjects = catalog()
    subjects[0]["history"][1]["sources"] = []
    result = run_case(tmp_path, history_case(), subjects)
    answer = finished(result)
    assert answer["outcome"] == "ANSWER"
    assert len(answer["sources"]) == 4
    assert len(queries(result, "CHARACTER_EVIDENCE")) == 4
    assert "(출처 2화)" in answer["text"]
    assert f"{LABEL}: 27 (출처 2화) · 원문 미확인" in answer["text"]
    assert answer["text"].count("원문 미확인") == 1
    assert NOTICES["NO_SOURCE"] in answer["limitations"]


def test_all_sources_unavailable_still_returns_every_registered_history_record(tmp_path):
    subjects = catalog()
    for evidence in subjects[0]["evidence"]:
        evidence["evidence"].update(status="SOURCE_PURGED", matchesCurrentSource=False, quotes=[])
    result = run_case(tmp_path, history_case(), subjects)
    answer = finished(result)
    assert answer["outcome"] == "ANSWER"
    assert answer["sources"] == []
    assert len(queries(result, "CHARACTER_EVIDENCE")) == 5
    assert all(f"(출처 {episode}화)" in answer["text"] for episode in range(1, 6))
    assert answer["text"].count("원문 미확인") == 5
    assert NOTICES["SOURCE_PURGED"] in answer["limitations"]
    assert len(result["raw"].requests) == 1


def test_history_without_requested_sources_never_queries_or_marks_sources_missing(tmp_path):
    case = history_case()
    case["question"] = f"{NAME}의 {LABEL} 변경 이력을 전부 보여줘"
    case["outputs"][0]["includeEvidence"] = False
    result = run_case(tmp_path, case, catalog())
    answer = finished(result)
    assert answer["outcome"] == "ANSWER"
    assert answer["sources"] == queries(result, "CHARACTER_EVIDENCE") == []
    assert all(f"(출처 {episode}화)" in answer["text"] for episode in range(1, 6))
    assert "원문 미확인" not in answer["text"]
    assert NOTICES["NO_SOURCE"] not in answer["limitations"]
    assert NOTICES["EVIDENCE_LIMIT"] not in answer["limitations"]
    assert len(result["raw"].requests) == 1


@pytest.mark.parametrize("include_evidence", [False, True])
def test_single_current_value_marks_missing_source_only_when_requested(tmp_path, include_evidence):
    case = history_case(mode="CURRENT")
    case["question"] = f"{NAME}의 현재 {LABEL}은?" + (" 원문도 보여줘." if include_evidence else "")
    case["outputs"][0]["includeEvidence"] = include_evidence
    result = run_case(tmp_path, case, catalog())
    answer = finished(result)
    assert answer["outcome"] == "ANSWER"
    assert answer["text"].startswith(f"{NAME}의 현재 {LABEL}은 89입니다.")
    assert "(직접 입력한 설정)" in answer["text"]
    assert ("연결된 원문은 확인하지 못했어요." in answer["text"]) == include_evidence
    assert (NOTICES["NO_SOURCE"] in answer["limitations"]) == include_evidence
    assert answer["sources"] == queries(result, "CHARACTER_EVIDENCE") == []
    assert "출처" not in answer["text"]


def test_history_source_range_applies_to_metadata_values_and_each_direct_source(tmp_path):
    result = run_case(tmp_path, history_case(episode_from=2, episode_to=4), catalog())
    answer = finished(result)
    for tool in ["CHARACTER_FIELDS", "CHARACTER_HISTORY", "CHARACTER_EVIDENCE"]:
        assert queries(result, tool)
        assert all((q["episodeFrom"], q["episodeTo"]) == (2, 4) for q in queries(result, tool))
    assert "출처 1화" not in answer["text"] and "출처 5화" not in answer["text"]
    assert [r["targetId"] for r in answer["sources"]] == [uid(n) for n in [30002, 30003, 30004]]
    assert NOTICES["SOURCE_RANGE"] in answer["limitations"]


def test_evidence_limit_preserves_all_records_and_discloses_unchecked_sources(tmp_path):
    result = run_case(tmp_path, history_case(), catalog(), config={"max_evidence_calls": 2})
    answer = finished(result)
    assert len(queries(result, "CHARACTER_EVIDENCE")) == len(answer["sources"]) == 2
    assert all(f"(출처 {episode}화)" in answer["text"] for episode in range(1, 6))
    assert answer["text"].count("원문 미확인") == 3
    assert NOTICES["EVIDENCE_LIMIT"] in answer["limitations"]
    assert NOTICES["PARTIAL_RESULTS"] not in answer["limitations"]
    assert len(result["raw"].requests) == 1


def test_current_value_and_history_only_or_removal_records_remain_distinct(tmp_path):
    subjects = catalog()
    subjects[0]["history"][1]["historyOnly"] = True
    subjects[0]["history"][2]["operation"] = "REMOVE"
    result = run_case(tmp_path, history_case(mode="CURRENT_AND_HISTORY"), subjects)
    answer = finished(result)
    assert f"{LABEL}: 89 (현재 설정) · 직접 입력" in answer["text"]
    assert f"{LABEL}: 27 (출처 2화) · 현재 설정에 반영하지 않은 기록" in answer["text"]
    assert f"{LABEL}: 27 (출처 3화) · 삭제 기록" in answer["text"]
    assert len(answer["sources"]) == 5
    assert len(queries(result, "CHARACTER_CURRENT")) == 1
    assert len(result["raw"].requests) == 1


def test_repeated_value_is_retained_as_a_record_not_invented_as_an_increase(tmp_path):
    result = run_case(tmp_path, history_case(), catalog(values=("27", "27", "12")))
    answer = finished(result)
    assert f"{LABEL}: 27 (출처 1화)" in answer["text"]
    assert f"{LABEL}: 27 (출처 2화)" in answer["text"]
    assert f"{LABEL}: 12 (출처 3화)" in answer["text"]
    assert len(answer["sources"]) == 3
    assert "증가" not in answer["text"] and "상승" not in answer["text"]
    assert "현재" not in answer["text"]
    assert len(result["raw"].requests) == 1
