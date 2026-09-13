"""Offline identity rules and the boundary for adjacent-chunk LLM fallback."""

import asyncio
import json
import socket
from uuid import uuid4

import pytest

from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.ordered_character_subjects import (
    OrderedSubjectTarget,
    merge_ordered_subjects,
    resolve_ordered_character_subjects,
)
from app.analysis.schemas import ExtractedSettingCandidate
from app.llm.responses import LlmTextResponse


class RecordingClient:
    def __init__(self, response=None):
        self.response = response
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        assert self.response is not None, "A deterministic identity must not call the provider."
        return LlmTextResponse(text=json.dumps(self.response))


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def reject(*args, **kwargs):
        raise AssertionError("Identity rule tests must not access the network.")

    class OfflineEncoding:
        def encode(self, value, **kwargs):
            return list(value.encode("utf-8"))

    monkeypatch.setattr(socket.socket, "connect", reject)
    monkeypatch.setattr(
        "app.analysis.ordered_context.tiktoken.get_encoding", lambda unused: OfflineEncoding()
    )


def _setting(name, *, raw=None, quote=None):
    return ExtractedSettingCandidate(
        source_chunk_id=uuid4(),
        candidate_kind="SETTING",
        entity_name=name,
        raw_entity_mention=raw,
        attribute_name="level",
        attribute_value="12",
        value_type="NUMBER",
        value_json={"value": 12},
        evidence_spans=[{"quote": quote or f"{name}의 레벨은 12였다."}],
    )


def _discovery(name, *, raw=None, quote=None):
    return ExtractedSettingCandidate(
        source_chunk_id=uuid4(),
        candidate_kind="CHARACTER_DISCOVERY",
        entity_name=name,
        raw_entity_mention=raw,
        evidence_spans=[{"quote": quote or f"{name}이 나타났다."}],
    )


def _known(name="에르웬 미샤", *, aliases=(), provisional=False):
    return OrderedSubjectTarget(
        name=name,
        aliases=aliases,
        actual_character_id=None if provisional else uuid4(),
        provisional_subject_key=f"provisional-character:{uuid4()}" if provisional else None,
    )


def _resolve(candidates, *, subjects=(), previous=None, current="현재 청크.", next_text=None,
             response=None):
    client = RecordingClient(response)
    resolved, updates = asyncio.run(resolve_ordered_character_subjects(
        client=client,
        model="offline-test-model",
        max_attempts=1,
        max_output_tokens=2000,
        context=SubjectResolutionChunkContext(previous, current, next_text),
        candidates=candidates,
        subjects=list(subjects),
        episode_no=2,
    ))
    return resolved, updates, client


def _assert_binding(item, target):
    assert item.binding.match_status == "MATCHED"
    assert item.binding.actual_character_id == target.actual_character_id
    assert item.binding.provisional_subject_key == target.provisional_subject_key


def _assert_ambiguous(item):
    assert item.binding.match_status == "AMBIGUOUS"
    assert item.binding.actual_character_id is None
    assert item.binding.provisional_subject_key is None


@pytest.mark.parametrize("provisional", [False, True])
@pytest.mark.parametrize("name", ["에르웬 미샤", "에르웬", "은빛 궁수"])
def test_known_name_alias_and_unique_short_name_resolve_without_llm(name, provisional):
    subject = _known(aliases=("은빛 궁수",), provisional=provisional)
    candidate = _setting(name, raw="그녀는")
    resolved, updates, client = _resolve([candidate], subjects=[subject])

    _assert_binding(resolved[0], subject)
    assert resolved[0].candidate == candidate
    assert updates == []
    assert client.requests == []


@pytest.mark.parametrize("subjects,name", [
    ([_known(), _known()], "에르웬 미샤"),
    ([_known("에르웬 미샤"), _known("에르웬 라프손")], "에르웬"),
    ([_known("에르웬", aliases=("은빛 궁수",)),
      _known("세룸", aliases=("은빛 궁수",))], "은빛 궁수"),
    ([_known("에르웬"), _known("세룸", aliases=("에르웬",))], "에르웬"),
])
def test_competing_names_and_aliases_stay_ambiguous_without_guessing(subjects, name):
    resolved, updates, client = _resolve([_setting(name)], subjects=subjects)

    _assert_ambiguous(resolved[0])
    assert updates == []
    assert client.requests == []


def test_raw_name_and_extracted_name_disagreement_stays_ambiguous():
    subjects = [_known("에르웬"), _known("세룸")]
    resolved, updates, client = _resolve(
        [_setting("에르웬", raw="세룸")], subjects=subjects
    )

    _assert_ambiguous(resolved[0])
    assert updates == []
    assert client.requests == []


def test_same_alias_on_one_identity_does_not_create_a_false_collision():
    subject = _known("에르웬", aliases=("에르웬", "에르웬 미샤"))
    resolved, _, client = _resolve([_setting("에르웬")], subjects=[subject])

    _assert_binding(resolved[0], subject)
    assert client.requests == []


def test_new_discovery_and_settings_share_anchor_even_if_setting_appears_first():
    candidates = [_setting("세룸"), _discovery("세룸"), _setting("세룸")]
    resolved, updates, client = _resolve(candidates)

    assert len(resolved) == 3
    assert len(updates) == 1
    expected_key = f"provisional-character:{resolved[1].binding.candidate_id}"
    assert updates[0].provisional_subject_key == expected_key
    for item in resolved:
        _assert_binding(item, updates[0])
    assert [item.candidate for item in resolved] == candidates
    assert client.requests == []


def test_duplicate_discoveries_preserve_all_evidence_and_one_provisional_anchor():
    candidates = [
        _discovery("에르웬", quote="에르웬이 등장했다."),
        _discovery("에르웬", quote="에르웬이 문을 닫았다."),
        _discovery("에르웬 미샤", quote="그녀의 이름은 에르웬 미샤였다."),
        _setting("에르웬 미샤"),
    ]
    resolved, updates, client = _resolve(candidates)

    assert len(resolved) == len(candidates)
    assert len(updates) == 1
    subject = updates[0]
    for item in resolved:
        _assert_binding(item, subject)
    assert {subject.name, *subject.aliases} == {"에르웬", "에르웬 미샤"}
    assert set(subject.evidence) == {item.evidence_spans[0].quote for item in candidates[:3]}
    assert [item.candidate for item in resolved] == candidates
    assert client.requests == []


def test_known_rediscovery_preserves_alias_evidence_and_enriches_later_rules():
    subject = _known("에르웬")
    candidate = _discovery("에르웬 미샤", quote="그녀의 이름은 에르웬 미샤였다.")
    resolved, updates, client = _resolve([candidate], subjects=[subject])

    _assert_binding(resolved[0], subject)
    assert resolved[0].candidate == candidate
    assert len(updates) == 1
    assert "에르웬 미샤" in updates[0].aliases
    assert candidate.evidence_spans[0].quote in updates[0].evidence
    subjects = [subject]
    merge_ordered_subjects(subjects, updates)
    merge_ordered_subjects(subjects, updates)
    assert len(subjects) == 1
    later, _, later_client = _resolve([_setting("에르웬 미샤")], subjects=subjects)
    _assert_binding(later[0], subject)
    assert client.requests == later_client.requests == []


def test_discovery_relationship_phrase_does_not_bind_child_to_existing_parent():
    father = _known("케닉")
    candidate = _discovery("세룸", raw="케닉의 넷째 아들 세룸")
    resolved, updates, client = _resolve([candidate], subjects=[father])

    assert len(updates) == 1
    assert updates[0].name == "세룸"
    assert updates[0].actual_character_id is None
    _assert_binding(resolved[0], updates[0])
    assert client.requests == []


def test_ambiguous_discovery_does_not_persist_an_alias_or_invent_an_anchor():
    subjects = [_known("에르웬 미샤"), _known("에르웬 라프손")]
    resolved, updates, client = _resolve([_discovery("에르웬")], subjects=subjects)

    _assert_ambiguous(resolved[0])
    assert updates == []
    assert client.requests == []


@pytest.mark.parametrize("name", ["미상", "그녀", "나", "등록되지않은이름"])
def test_unresolved_single_chunk_setting_stays_ambiguous_without_llm(name):
    resolved, updates, client = _resolve([_setting(name, raw="그녀는")])

    _assert_ambiguous(resolved[0])
    assert updates == []
    assert client.requests == []


@pytest.mark.parametrize("adjacent", [None, "", " \n\t ", "현재 청크."])
def test_empty_or_repeated_adjacent_text_does_not_justify_another_llm_call(adjacent):
    resolved, _, client = _resolve(
        [_setting("미상")], subjects=[_known()], previous=adjacent, next_text=adjacent
    )

    _assert_ambiguous(resolved[0])
    assert client.requests == []


def test_adjacent_fallback_only_receives_unresolved_original_candidate_reference():
    known = _known("에르웬")
    candidates = [_setting("에르웬"), _setting("미상", raw="그녀는")]
    resolved, updates, client = _resolve(
        candidates,
        subjects=[known],
        previous="에르웬이 자신을 가리켰다.",
        response={"resolutions": [{"candidate_ref": "C2", "target_ref": "K1"}]},
    )

    for item in resolved:
        _assert_binding(item, known)
    assert [item.candidate for item in resolved] == candidates
    assert updates == []
    assert len(client.requests) == 1
    request = client.requests[0]
    prompt = json.loads(request["user_prompt"])
    assert [item["ref"] for item in prompt["candidates"]] == ["C2"]
    assert prompt["context"]["previous_chunk"] == "에르웬이 자신을 가리켰다."
    assert str(known.actual_character_id) not in request["user_prompt"]


def test_adjacent_fallback_can_link_unresolved_setting_to_new_discovery_anchor():
    candidates = [_setting("미상", raw="그녀는"), _discovery("세룸")]
    resolved, updates, client = _resolve(
        candidates,
        next_text="그녀가 자신을 세룸이라고 소개했다.",
        response={"resolutions": [{"candidate_ref": "C1", "target_ref": "D2"}]},
    )

    assert len(updates) == 1
    for item in resolved:
        _assert_binding(item, updates[0])
    assert len(client.requests) == 1
    prompt = json.loads(client.requests[0]["user_prompt"])
    assert [item["ref"] for item in prompt["candidates"]] == ["C1"]
    assert any(item["ref"] == "D2" and item["name"] == "세룸" for item in prompt["targets"])
    assert updates[0].provisional_subject_key not in client.requests[0]["user_prompt"]


def test_adjacent_fallback_null_keeps_candidate_without_creating_an_identity():
    candidate = _setting("미상", raw="그녀는")
    resolved, updates, client = _resolve(
        [candidate],
        subjects=[_known()],
        previous="여러 사람이 서로를 바라보았다.",
        response={"resolutions": [{"candidate_ref": "C1", "target_ref": None}]},
    )

    _assert_ambiguous(resolved[0])
    assert resolved[0].candidate == candidate
    assert updates == []
    assert len(client.requests) == 1


def test_adjacent_text_without_any_selectable_identity_does_not_call_llm():
    candidate = _setting("미상", raw="그녀는")
    resolved, updates, client = _resolve(
        [candidate], previous="그녀가 자신을 세룸이라고 소개했다."
    )

    _assert_ambiguous(resolved[0])
    assert resolved[0].candidate == candidate
    assert updates == []
    assert client.requests == []


def test_two_long_discoveries_keep_distinct_anchors_and_shared_short_name_is_ambiguous():
    candidates = [
        _discovery("에르웬"),
        _discovery("에르웬 미샤"),
        _discovery("에르웬 라프손"),
        _setting("에르웬"),
        _setting("에르웬 미샤"),
        _setting("에르웬 라프손"),
    ]
    resolved, updates, client = _resolve(candidates)

    _assert_ambiguous(resolved[0])
    _assert_ambiguous(resolved[3])
    assert len(updates) == 2
    by_name = {subject.name: subject for subject in updates}
    _assert_binding(resolved[1], by_name["에르웬 미샤"])
    _assert_binding(resolved[4], by_name["에르웬 미샤"])
    _assert_binding(resolved[2], by_name["에르웬 라프손"])
    _assert_binding(resolved[5], by_name["에르웬 라프손"])
    assert by_name["에르웬 미샤"].provisional_subject_key != (
        by_name["에르웬 라프손"].provisional_subject_key
    )
    assert all("에르웬" not in subject.aliases for subject in updates)
    assert client.requests == []


@pytest.mark.parametrize("name", ["미상", "그녀", "나"])
def test_invalid_pronoun_discovery_never_becomes_a_provisional_character(name):
    candidate = _discovery(name)
    resolved, updates, client = _resolve(
        [candidate], subjects=[_known()], previous="다른 장면의 추가 문맥."
    )

    _assert_ambiguous(resolved[0])
    assert updates == []
    assert client.requests == []


def test_one_character_name_does_not_partially_match_a_longer_name():
    resolved, updates, client = _resolve([_setting("김")], subjects=[_known("김철수")])

    _assert_ambiguous(resolved[0])
    assert updates == []
    assert client.requests == []


def test_matched_setting_with_adjacent_context_still_skips_llm():
    subject = _known("에르웬")
    resolved, _, client = _resolve(
        [_setting("에르웬")], subjects=[subject], previous="앞 청크에도 에르웬이 등장했다."
    )

    _assert_binding(resolved[0], subject)
    assert client.requests == []


@pytest.mark.parametrize("adjacent_side", ["previous", "next_text"])
def test_adjacent_excerpt_already_inside_current_chunk_is_not_extra_context(adjacent_side):
    excerpt = "에르웬이 나타났다."
    resolved, updates, client = _resolve(
        [_setting("미상", raw="그녀는")],
        subjects=[_known("에르웬")],
        current=f"문이 열렸다.\n{excerpt}\n그녀의 레벨은 12였다.",
        **{adjacent_side: f" \n{excerpt}\n "},
    )

    _assert_ambiguous(resolved[0])
    assert updates == []
    assert client.requests == []


@pytest.mark.parametrize("provisional", [False, True])
@pytest.mark.parametrize("name,aliases,discoveries", [
    ("에르웬 미샤", (), ("에르웬", "미샤")),
    ("에르웬", ("에르웬 미샤",), ("에르웬", "미샤")),
    ("은빛 궁수", ("에르웬 미샤",), ("에르웬", "미샤")),
    ("에르웬 미샤 라프손", ("에르웬",), ("에르웬 미샤", "라프손")),
    ("에르웬 미샤", (), ("미샤", "에르웬 미샤 라프손")),
])
def test_shortened_known_names_are_not_competing_full_names(
    name, aliases, discoveries, provisional,
):
    subject = _known(name, aliases=aliases, provisional=provisional)
    candidates = [*map(_discovery, discoveries), _setting(name)]
    resolved, updates, client = _resolve(candidates, subjects=[subject])

    for item in resolved:
        _assert_binding(item, subject)
    assert [item.candidate for item in resolved] == candidates
    assert len(updates) == 1
    assert set(updates[0].aliases) == (set(aliases) | set(discoveries)) - {name}
    assert set(updates[0].evidence) == {
        candidate.evidence_spans[0].quote for candidate in candidates[:-1]
    }
    assert client.requests == []


@pytest.mark.parametrize("provisional", [False, True])
def test_competing_new_full_names_do_not_merge_into_one_registered_short_name(provisional):
    subject = _known("에르웬", provisional=provisional)
    candidates = [
        _discovery("에르웬 미샤"),
        _discovery("에르웬 라프손"),
        _setting("에르웬"),
        _setting("에르웬 미샤"),
        _setting("에르웬 라프손"),
    ]
    resolved, updates, client = _resolve(candidates, subjects=[subject])

    for item in resolved:
        _assert_ambiguous(item)
    assert [item.candidate for item in resolved] == candidates
    assert updates == []
    assert client.requests == []


@pytest.mark.parametrize("aliases", [
    ("에르웬 미샤",),
    ("에르웬 미샤", "에르웬 라프손"),
])
def test_registered_aliases_do_not_count_as_competing_new_full_names(aliases):
    subject = _known("에르웬", aliases=aliases)
    candidates = [
        _discovery("에르웬 미샤"),
        _discovery("에르웬 라프손"),
        _setting("에르웬"),
    ]
    resolved, updates, client = _resolve(candidates, subjects=[subject])

    for item in resolved:
        _assert_binding(item, subject)
    assert len(updates) == 1
    assert set(updates[0].aliases) == {"에르웬 미샤", "에르웬 라프손"}
    assert set(updates[0].evidence) == {
        candidate.evidence_spans[0].quote for candidate in candidates[:2]
    }
    assert client.requests == []
