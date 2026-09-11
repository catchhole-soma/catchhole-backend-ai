import asyncio
import json
from copy import deepcopy
from uuid import UUID

import pytest

from app.analysis.character_identity_resolver import (
    _identity_error_summary,
    reconcile_episode_names,
)
from app.analysis.character_name_resolver import (
    ActiveCharacterStatus,
    KnownCharacter,
    normalize_known_characters,
    resolve_candidate_character,
)
from app.analysis.exceptions import LlmExtractionError
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.responses import LlmTextResponse
from app.usage.metering import MeteredTextGenerationClient


class SequenceClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    async def create_text_response(self, **kwargs):
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LlmTextResponse(
            text=json.dumps(response, ensure_ascii=False), input_token_count=50, output_token_count=20,
        )


def _candidate(name, *, raw=None, discovery=False, chunk=1, quote="후보의 고정 근거", start=10):
    return ExtractedSettingCandidate(
        source_chunk_id=UUID(int=chunk), entity_name=name, raw_entity_mention=raw or name,
        candidate_kind="CHARACTER_DISCOVERY" if discovery else "SETTING",
        attribute_name=None if discovery else "status.발_부상",
        attribute_value=None if discovery else "PRIVATE_STATE_SENTINEL",
        value_type=None if discovery else "JSON",
        value_json=None if discovery else {"name": "부상", "active": True},
        evidence_spans=[ExtractedEvidenceSpan(
            quote=quote, start_offset=start, end_offset=start + len(quote),
        )],
        confidence=0.91,
    )


def _pair(ref="R1", relation="UNRESOLVED", *, quote=None, source="current_episode"):
    return {
        "pair_ref": ref, "relation": relation,
        "evidence": [] if quote is None else [{"source": source, "quote": quote}],
    }


def _response(*decisions):
    return {"decisions": list(decisions)}


def _resolve(client, candidates, *, current="현재 회차", previous=None, known=()):
    return asyncio.run(reconcile_episode_names(
        llm_client=client, model="gpt-5.6-luna", max_output_tokens=2000,
        context_episode_text=current, previous_episode_text=previous,
        candidates=candidates, known_characters=list(known),
    ))


def test_aliases_share_one_new_name_without_changing_any_fact_metadata():
    quote = "레이의 딸 세나가 고개를 들었다. 세나라고 부르자 그녀가 대답했다."
    candidates = [
        _candidate("세나", discovery=True, quote=quote, start=10),
        _candidate("레이의 딸 세나", chunk=2, quote=quote, start=100),
        _candidate("레이의 딸 세나", discovery=True, chunk=3, quote=quote, start=200),
        _candidate("미상"), _candidate("테오"),
    ]
    before = deepcopy(candidates)
    client = SequenceClient([_response(_pair(relation="SAME_PERSON", quote=quote))])

    result = _resolve(client, candidates, current=quote, known=[KnownCharacter(UUID(int=9), "테오")])

    assert result.call_count == 1
    assert result.renamed_candidate_count == 2
    assert result.unresolved_candidate_count == 0
    assert [c.entity_name for c in result.candidates] == ["세나", "세나", "세나", "미상", "테오"]
    _assert_only_names_changed(before, candidates, result.candidates)
    assert result.candidates[3] is candidates[3]
    assert result.candidates[4] is candidates[4]
    request = client.requests[0]
    payload = json.loads(request["user_prompt"])
    assert payload["pairs"] == [{"pair_ref": "R1", "left_ref": "N1", "right_ref": "N2"}]
    occurrence = payload["new_names"][1]["occurrences"][0]
    assert occurrence == {
        "occurrence_ref": "O2", "chunk_ref": "H2", "raw_mention": "레이의 딸 세나",
        "evidence_spans": [{"quote": quote, "start_offset": 100, "end_offset": 100 + len(quote)}],
    }
    for private in ["PRIVATE_STATE_SENTINEL", "source_chunk_id", "attribute_name", "active_statuses"]:
        assert private not in request["user_prompt"]
    assert request["model"] == "gpt-5.6-luna"
    assert request["prompt_cache_key"] == "character-identity-resolution:v3"
    assert request["response_schema"].strict is True


@pytest.fixture
def trial02_split_names():
    # Minimal shape from 83c4bf trial-02 episode-02: general extraction used the
    # short name, then subject resolution renamed only the first-person item.
    # These names both appear in the source; name-existence checks cannot merge them.
    return [
        _candidate("비요른", raw="얀델의 아들 비요른", discovery=True,
                   quote="“얀델의 아들 비요른은 나와라!”", start=10),
        _candidate("비요른", raw="비요른 얀델", quote="「비요른 얀델」", start=300),
        _candidate("비요른 얀델", raw="나는", quote="‘비요른 얀델.’", start=200, chunk=2),
    ]


@pytest.mark.parametrize("relation", ["SAME_PERSON", "UNRESOLVED"])
def test_trial02_subject_name_split_is_merged_or_held_not_approved_as_two_people(
    trial02_split_names, relation,
):
    quote = "‘비요른 얀델.’\n앞으로 나는 이 이름으로 살아야 한다."
    candidates = trial02_split_names
    before = deepcopy(candidates)
    current = "“얀델의 아들 비요른은 나와라!”\n" + quote + "\n「비요른 얀델」"
    client = SequenceClient([_response(_pair(
        relation=relation, quote=quote if relation == "SAME_PERSON" else None,
    ))])
    result = _resolve(client, candidates, current=current)
    expected_name = "비요른" if relation == "SAME_PERSON" else "미상"
    assert [c.entity_name for c in result.candidates] == [expected_name] * 3
    assert result.unresolved_candidate_count == (3 if relation == "UNRESOLVED" else 0)
    if relation == "UNRESOLVED":
        assert all(resolve_candidate_character(c, []).match_status == "AMBIGUOUS"
                   for c in result.candidates)
    _assert_only_names_changed(before, candidates, result.candidates)
    assert json.loads(client.requests[0]["user_prompt"])["new_names"][1]["occurrences"][0][
        "raw_mention"
    ] == "나는"


def test_alias_can_use_previous_episode_identity_proof_and_known_ref():
    quote = "리아 미르를 사람들은 흰매라고 불렀다."
    known = KnownCharacter(
        UUID(int=8), "리아 미르", (ActiveCharacterStatus("status.숨김", "비공개 상태"),),
    )
    candidates = [_candidate("흰매"), _candidate("리아 미르"), _candidate("주인공")]
    client = SequenceClient([_response(_pair(
        relation="SAME_PERSON", quote=quote, source="previous_episode",
    ))])
    result = _resolve(client, candidates, previous=quote, known=[known])
    assert [c.entity_name for c in result.candidates] == ["리아 미르", "리아 미르", "주인공"]
    assert result.renamed_candidate_count == 1
    assert result.candidates[1] is candidates[1]
    assert "비공개 상태" not in client.requests[0]["user_prompt"]
    assert str(known.character_id) not in client.requests[0]["user_prompt"]


@pytest.mark.parametrize("source", ["current_episode", "previous_episode"])
def test_identity_evidence_accepts_crlf_to_lf_and_internal_whitespace(source):
    original = "레이의 딸 세나가 왔다.\r\n\r\n세나는   같은 사람이었다."
    quote = "레이의 딸 세나가 왔다.\n\n세나는 같은 사람이었다."
    client = SequenceClient([_response(_pair(relation="SAME_PERSON", quote=quote, source=source))])
    result = _resolve(
        client, [_candidate("세나"), _candidate("레이의 딸 세나")],
        current=original if source == "current_episode" else "현재 회차",
        previous=original if source == "previous_episode" else None,
    )
    assert result.call_count == 1
    assert [c.entity_name for c in result.candidates] == ["세나", "세나"]


@pytest.mark.parametrize("quote", [
    " 세나는 레이의 딸이다. ",
    "세나는 레이의 아들이다.",
    "세나는 레이의 딸이다. 그녀가 대답했다.",
    "   ",
])
def test_identity_evidence_rejects_padding_alteration_and_joined_noncontiguous_sentences(quote):
    original = "세나는 레이의 딸이다. 다른 사람이 먼저 이야기했다. 그녀가 대답했다."
    response = _response(_pair(relation="SAME_PERSON", quote=quote))
    client = SequenceClient([response, response])
    with pytest.raises(LlmExtractionError, match="IDENTITY_EVIDENCE_NOT_VERBATIM"):
        _resolve(client, [_candidate("세나"), _candidate("레이의 딸 세나")], current=original)
    feedback = json.loads(client.requests[1]["user_prompt"])["validation_feedback"]
    assert feedback["reason"] == "IDENTITY_EVIDENCE_NOT_VERBATIM"


def test_identity_retry_summary_only_preserves_exact_local_error_codes():
    assert _identity_error_summary(ValueError("IDENTITY_PAIR_COVERAGE")) == "IDENTITY_PAIR_COVERAGE"
    for message in [
        "PRIVATE_RESPONSE_SENTINEL", "IDENTITY_PAIR_COVERAGE: PRIVATE_RESPONSE_SENTINEL",
        "IDENTITY_UNKNOWN_EXTERNAL_CODE",
    ]:
        summary = _identity_error_summary(ValueError(message))
        assert summary == "ValueError"
        assert message not in summary


@pytest.mark.parametrize("candidates,known", [
    ([], []),
    ([_candidate("미상"), _candidate("그는")], []),
    ([_candidate("리아 미르")], [KnownCharacter(UUID(int=8), "리아 미르")]),
    ([_candidate("리아")], [KnownCharacter(UUID(int=8), "리아 미르")]),
    ([_candidate("세나")], []),
    ([_candidate("세나"), _candidate("테오")], []),
])
def test_skips_without_new_unmatched_names_or_conflicting_pairs(candidates, known):
    client = SequenceClient([])
    result = _resolve(client, candidates, known=known)
    assert result.candidates is candidates
    assert result.call_count == result.renamed_candidate_count == result.unresolved_candidate_count == 0
    assert client.requests == []


def test_ambiguous_partial_name_is_held_instead_of_automatically_merged():
    client = SequenceClient([_response(_pair("R1"), _pair("R2"))])
    candidates = [_candidate("리아")]
    known = [KnownCharacter(UUID(int=8), "리아 미르"), KnownCharacter(UUID(int=9), "리아 노엘")]
    result = _resolve(client, candidates, known=known)
    assert result.call_count == 1
    assert result.renamed_candidate_count == 0
    assert result.unresolved_candidate_count == 1
    assert result.candidates[0].entity_name == "미상"
    assert resolve_candidate_character(
        result.candidates[0], normalize_known_characters(known),
    ).match_status == "AMBIGUOUS"


def test_name_replacement_does_not_touch_already_matched_occurrences_of_the_same_name():
    quote = "리아 미르는 흰매라는 별명을 쓴다."
    candidates = [_candidate("흰매", raw="리아 미르"), _candidate("흰매")]
    client = SequenceClient([_response(_pair(relation="SAME_PERSON", quote=quote))])
    result = _resolve(client, candidates, current=quote, known=[KnownCharacter(UUID(int=8), "리아 미르")])
    assert result.candidates[0] is candidates[0]
    assert result.candidates[0].entity_name == "흰매"
    assert result.candidates[1].entity_name == "리아 미르"
    assert result.renamed_candidate_count == 1


@pytest.mark.parametrize("left,right,quote", [
    ("카두아", "카두아의 아들 오름", "카두아가 아들 오름에게 인사했다."),
    ("리아", "리아나", "리아와 리아나가 서로 마주 보고 인사했다."),
    ("기사 리아", "사서 리아", "기사 리아와 사서 리아는 이름만 같은 다른 사람이다."),
])
def test_distinct_people_require_positive_source_proof_and_remain_separate(left, right, quote):
    candidates = [_candidate(left), _candidate(right)]
    client = SequenceClient([_response(_pair(relation="DISTINCT_PERSON", quote=quote))])
    result = _resolve(client, candidates, current=quote)
    assert result.candidates == candidates
    assert result.renamed_candidate_count == result.unresolved_candidate_count == 0


@pytest.mark.parametrize("response", [
    _response(),
    _response(_pair(), _pair()),
    _response(_pair("R99")),
    _response(_pair(relation="SAME_PERSON")),
    _response(_pair(relation="DISTINCT_PERSON")),
    _response(_pair(relation="SAME_PERSON", quote="원문에 없는 인용")),
    _response(_pair(relation="DISTINCT_PERSON", quote="신원 근거", source="previous_episode")),
    _response(_pair(relation=True, quote="신원 근거")),
    _response({**_pair(), "canonical_ref": "N1"}),
])
def test_invalid_relation_fails_atomically_after_two_attempts(response):
    client = SequenceClient([response, response])
    candidates = [_candidate("세나"), _candidate("레이의 딸 세나")]
    before = deepcopy(candidates)
    with pytest.raises(LlmExtractionError, match="failed after 2 attempts"):
        _resolve(client, candidates, current="신원 근거")
    assert len(client.requests) == 2
    assert candidates == before
    assert json.loads(client.requests[1]["user_prompt"])["validation_feedback"][
        "previous_response_rejected"
    ]


def test_transitive_same_person_conflicting_with_distinct_person_is_rejected():
    quote = "신원 근거"
    response = _response(
        _pair("R1", "SAME_PERSON", quote=quote),
        _pair("R2", "DISTINCT_PERSON", quote=quote),
        _pair("R3", "SAME_PERSON", quote=quote),
    )
    client = SequenceClient([response, response])
    with pytest.raises(LlmExtractionError, match="failed after 2 attempts"):
        _resolve(client, [_candidate("리아"), _candidate("리아나"), _candidate("리아나라")],
                 current=quote)


def test_same_person_cannot_merge_two_existing_character_roots():
    quote = "신원 근거"
    response = _response(
        _pair("R1", "SAME_PERSON", quote=quote), _pair("R2", "SAME_PERSON", quote=quote),
    )
    client = SequenceClient([response, response])
    with pytest.raises(LlmExtractionError, match="failed after 2 attempts"):
        _resolve(client, [_candidate("리아")], current=quote, known=[
            KnownCharacter(UUID(int=8), "리아 미르"), KnownCharacter(UUID(int=9), "리아 노엘"),
        ])


def test_unresolved_component_is_held_without_touching_unrelated_names():
    client = SequenceClient([_response(_pair())])
    candidates = [_candidate("세나"), _candidate("레이의 딸 세나"), _candidate("테오")]
    before = deepcopy(candidates)
    result = _resolve(client, candidates)
    assert [c.entity_name for c in result.candidates] == ["미상", "미상", "테오"]
    assert result.unresolved_candidate_count == 2
    assert result.candidates[2] is candidates[2]
    _assert_only_names_changed(before, candidates, result.candidates)


def test_proven_known_alias_is_kept_while_unresolved_new_neighbor_is_held():
    quote = "리아 미르는 흰매다. 흰매와 흰매의 제자의 관계는 더 알려지지 않았다."
    candidates = [_candidate("흰매"), _candidate("흰매의 제자")]
    client = SequenceClient([_response(
        _pair("R1", "SAME_PERSON", quote=quote), _pair("R2"), _pair("R3"),
    )])
    result = _resolve(client, candidates, current=quote, known=[KnownCharacter(UUID(int=8), "리아 미르")])
    assert [c.entity_name for c in result.candidates] == ["리아 미르", "미상"]
    assert result.renamed_candidate_count == result.unresolved_candidate_count == 1


def test_same_person_transitivity_uses_earliest_source_occurrence_as_display_only():
    quote = "신원 근거"
    candidates = [
        _candidate("리아나라", start=300), _candidate("리아나", start=200),
        _candidate("리아", start=100),
    ]
    client = SequenceClient([_response(
        _pair("R1", "SAME_PERSON", quote=quote), _pair("R2"),
        _pair("R3", "SAME_PERSON", quote=quote),
    )])
    result = _resolve(client, candidates, current=quote)
    assert [c.entity_name for c in result.candidates] == ["리아"] * 3
    assert result.renamed_candidate_count == 2
    assert result.unresolved_candidate_count == 0


def test_raw_name_bridge_selects_pair_without_using_partial_name_as_proof():
    candidates = [_candidate("흰매", raw="리아 미르"), _candidate("리아 미르")]
    client = SequenceClient([_response(_pair())])
    result = _resolve(client, candidates)
    assert [c.entity_name for c in result.candidates] == ["미상", "미상"]
    assert result.unresolved_candidate_count == 2


def _assert_only_names_changed(before, originals, updated):
    assert originals == before
    assert len(updated) == len(originals)
    for original, replacement in zip(before, updated, strict=True):
        assert replacement.model_dump(exclude={"entity_name"}) == original.model_dump(
            exclude={"entity_name"},
        )


class Ledger:
    def __init__(self, *, reject_second=False):
        self.reservations = []
        self.settlements = []
        self.reject_second = reject_second

    async def reserve_ai_tokens(self, **kwargs):
        self.reservations.append(kwargs)
        if self.reject_second and len(self.reservations) == 2:
            raise AiTokenQuotaExhaustedError()

    async def settle_ai_tokens(self, **kwargs):
        self.settlements.append(kwargs)

    async def release_ai_tokens(self, **kwargs):
        raise AssertionError("Completed provider responses must settle their actual usage.")


def _metered(client, ledger):
    return MeteredTextGenerationClient(
        delegate=client, ledger=ledger, analysis_job_id=UUID(int=10),
        purpose="CHARACTER_SUBJECT_RESOLUTION", default_model="gpt-5.6-luna",
        lease_token=UUID(int=11), max_retries=0,
    )


def test_retry_reuses_metered_boundary_and_counts_both_calls():
    client = SequenceClient([_response(), _response(_pair())])
    ledger = Ledger()
    result = _resolve(_metered(client, ledger), [_candidate("세나"), _candidate("큰 세나")])
    assert result.call_count == 2
    assert result.unresolved_candidate_count == 2
    assert len(ledger.reservations) == len(ledger.settlements) == 2
    assert [item["attempt"] for item in ledger.reservations] == [1, 2]
    assert all(item["purpose"] == "CHARACTER_SUBJECT_RESOLUTION" for item in ledger.reservations)
    assert all(item["lease_token"] == UUID(int=11) for item in ledger.reservations)


def test_quota_rejection_does_not_call_provider_or_return_unreconciled_candidates():
    client = SequenceClient([_response()])
    ledger = Ledger(reject_second=True)
    with pytest.raises(AiTokenQuotaExhaustedError):
        _resolve(_metered(client, ledger), [_candidate("세나"), _candidate("큰 세나")])
    assert len(client.requests) == 1
    assert len(ledger.reservations) == 2
    assert len(ledger.settlements) == 1


def test_truncation_propagates_without_identity_retry_or_fallback():
    error = LlmOutputTruncatedError(
        "truncated", incomplete_reason="max_output_tokens", max_output_tokens=2000,
    )
    client = SequenceClient([error])
    with pytest.raises(LlmOutputTruncatedError):
        _resolve(client, [_candidate("세나"), _candidate("큰 세나")])
    assert len(client.requests) == 1
