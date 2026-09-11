import asyncio
import json
from pathlib import Path
from uuid import UUID

import pytest

from app.analysis.character_name_resolver import (
    KnownCharacter,
    normalize_known_characters,
    resolve_candidate_character,
)
from app.analysis.character_subject_resolver import (
    CharacterSubjectResolver,
    SubjectResolutionChunkContext,
)
from app.analysis.exceptions import LlmExtractionError
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.llm.responses import LlmTextResponse

BJORN_ID = UUID("00000000-0000-0000-0000-000000000101")
AINAR_ID = UUID("00000000-0000-0000-0000-000000000102")
CHUNK_ID = UUID("00000000-0000-0000-0000-000000000201")


def test_resolve_candidates_skips_llm_when_fallback_targets_do_not_exist(tmp_path: Path) -> None:
    # 원문 지칭도 구체 이름이면 subject resolver LLM을 추가 호출하지 않는다.
    llm_client = FakeSubjectResolutionClient(response_text='{"resolutions":[]}')
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )
    candidates = [_candidate(entity_name="비요른", raw_entity_mention="비요른")]

    result = _resolve(
        resolver,
        context=_context(),
        candidates=candidates,
        known_characters=[KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")],
    )

    assert llm_client.call_count == 0
    assert result.candidates == candidates
    assert result.fallback_call_count == 0
    assert result.fallback_resolved_count == 0
    assert result.fallback_unresolved_count == 0


def test_resolve_candidates_does_not_reinterpret_character_discovery(tmp_path: Path) -> None:
    llm_client = FakeSubjectResolutionClient(response_text='{"resolutions":[]}')
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )
    discovery = ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        candidate_kind="CHARACTER_DISCOVERY",
        entity_type="CHARACTER",
        entity_name="세룸",
        raw_entity_mention="케닉의 넷째 아들 세룸",
        attribute_name=None,
        attribute_value=None,
        value_type=None,
        value_json=None,
        evidence_spans=[ExtractedEvidenceSpan(quote="케닉의 넷째 아들 세룸은 나와라!")],
        confidence=0.95,
    )

    result = _resolve(
        resolver,
        context=_context(),
        candidates=[discovery],
        known_characters=[],
    )

    assert llm_client.call_count == 0
    assert result.candidates == [discovery]


@pytest.mark.parametrize("character_name", ["나은", "그로"])
def test_resolve_candidates_treats_exact_known_name_as_concrete_before_particle_check(
    tmp_path: Path,
    character_name: str,
) -> None:
    # "나은"의 "은"을 조사로 제거해 "나"라는 지칭어로 오인하지 않는다.
    llm_client = FakeSubjectResolutionClient(response_text='{"resolutions":[]}')
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )
    candidates = [_candidate(entity_name=character_name, raw_entity_mention=character_name)]

    result = _resolve(
        resolver,
        context=_context(),
        candidates=candidates,
        known_characters=[KnownCharacter(character_id=AINAR_ID, name=character_name)],
    )

    assert llm_client.call_count == 0
    assert result.candidates == candidates


def test_resolve_candidates_accepts_exact_known_name_from_fallback(
    tmp_path: Path,
) -> None:
    # fallback 결과가 조사형 지칭어처럼 보여도 기존 캐릭터 exact name이면 보존한다.
    llm_client = FakeSubjectResolutionClient(
        response_text="""
        {
          "resolutions": [
            {
              "candidate_id": "candidate-0",
              "resolved_entity_name": "그녀로",
              "reason": "문맥상 그녀로를 가리킨다."
            }
          ]
        }
        """
    )
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )

    result = _resolve(
        resolver,
        context=_context(),
        candidates=[_candidate(entity_name="미상", raw_entity_mention="그")],
        known_characters=[KnownCharacter(character_id=AINAR_ID, name="그녀로")],
    )

    assert result.candidates[0].entity_name == "그녀로"
    assert result.fallback_resolved_count == 1
    assert result.fallback_unresolved_count == 0


@pytest.mark.parametrize("character_name", ["나은", "그로"])
def test_resolve_candidates_preserves_particle_ending_new_name_from_fallback(
    tmp_path: Path,
    character_name: str,
) -> None:
    # fallback이 실제 이름이라고 재판단한 값은 조사형 지칭어와 겹쳐도 버리지 않는다.
    # 기존 캐릭터가 없으므로 최종 자동 연결은 하지 않고 AMBIGUOUS로 남는다.
    llm_client = FakeSubjectResolutionClient(
        response_text=f"""
        {{
          "resolutions": [
            {{
              "candidate_id": "candidate-0",
              "resolved_entity_name": "{character_name}",
              "reason": "문맥상 실제 캐릭터 이름이다."
            }}
          ]
        }}
        """
    )
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )

    result = _resolve(
        resolver,
        context=_context(),
        candidates=[
            _candidate(entity_name=character_name, raw_entity_mention=character_name)
        ],
        known_characters=[],
    )
    name_match = resolve_candidate_character(
        result.candidates[0],
        normalize_known_characters([]),
    )

    assert result.candidates[0].entity_name == character_name
    assert result.fallback_resolved_count == 1
    assert result.fallback_unresolved_count == 0
    assert name_match.match_status.value == "AMBIGUOUS"
    assert name_match.matched_character_id is None


def test_resolve_candidates_preserves_unresolved_placeholders_without_raw_mentions(
    tmp_path: Path,
) -> None:
    # raw 표현이 없더라도 청크 문맥으로 fallback을 시도하고, 특정하지 못한 후보는
    # 표준 placeholder로 유지해 이후 name resolver가 AMBIGUOUS로 저장하게 한다.
    llm_client = FakeSubjectResolutionClient(
        response_text="""
        {
          "resolutions": [
            {
              "candidate_id": "candidate-0",
              "resolved_entity_name": null,
              "reason": "주체를 특정할 수 없다."
            },
            {
              "candidate_id": "candidate-1",
              "resolved_entity_name": "그녀",
              "reason": "구체 이름을 찾지 못했다."
            },
            {
              "candidate_id": "candidate-2",
              "resolved_entity_name": "비요른",
              "reason": "원문 흐름에서 주체를 확인했다."
            }
          ]
        }
        """
    )
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )
    candidates = [
        _candidate(entity_name="미상", raw_entity_mention=None, attribute_name="level"),
        _candidate(entity_name="나", raw_entity_mention=None, attribute_name="status.각성"),
        _candidate(entity_name="비요른", raw_entity_mention=None, attribute_name="item.도끼"),
    ]

    result = _resolve(
        resolver,
        context=_context(),
        candidates=candidates,
        known_characters=[KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")],
    )

    assert llm_client.call_count == 1
    assert [candidate.attribute_name for candidate in result.candidates] == [
        "level",
        "status.각성",
        "item.도끼",
    ]
    assert [candidate.entity_name for candidate in result.candidates] == [
        "미상",
        "미상",
        "비요른",
    ]
    assert result.fallback_call_count == 1
    assert result.fallback_resolved_count == 1
    assert result.fallback_unresolved_count == 2


def test_resolve_candidates_batches_targets_and_preserves_unresolved_items(
    tmp_path: Path,
) -> None:
    # 같은 current chunk 안에서 나온 fallback 대상 후보들은 한 번의 LLM 호출로 함께 해소한다.
    llm_client = FakeSubjectResolutionClient(
        response_text="""
        {
          "resolutions": [
            {
              "candidate_id": "candidate-0",
              "resolved_entity_name": "비요른 얀델",
              "reason": "1인칭 서술 흐름이 비요른 얀델에게 이어진다."
            },
            {
              "candidate_id": "candidate-1",
              "resolved_entity_name": null,
              "reason": "그녀가 아이나르인지 다른 인물인지 확정할 수 없다."
            },
            {
              "candidate_id": "candidate-2",
              "resolved_entity_name": "미상",
              "reason": "주체를 특정할 수 없다."
            }
          ]
        }
        """
    )
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )
    candidates = [
        _candidate(entity_name="미상", raw_entity_mention="나는", attribute_name="level"),
        _candidate(entity_name="미상", raw_entity_mention="그녀는", attribute_name="item.검"),
        _candidate(entity_name="미상", raw_entity_mention="주인공", attribute_name="status.각성"),
        _candidate(entity_name="비요른", raw_entity_mention="비요른", attribute_name="status.부상"),
    ]

    result = _resolve(
        resolver,
        context=_context(),
        candidates=candidates,
        known_characters=[
            KnownCharacter(character_id=BJORN_ID, name="비요른 얀델"),
            KnownCharacter(character_id=AINAR_ID, name="아이나르"),
        ],
    )

    assert llm_client.call_count == 1
    assert "candidate-0" in llm_client.last_user_prompt
    assert "candidate-1" in llm_client.last_user_prompt
    assert "candidate-2" in llm_client.last_user_prompt
    assert "status.부상" not in llm_client.last_user_prompt
    assert [candidate.attribute_name for candidate in result.candidates] == [
        "level",
        "item.검",
        "status.각성",
        "status.부상",
    ]
    assert result.candidates[0].entity_name == "비요른 얀델"
    assert result.candidates[1].entity_name == "미상"
    assert result.candidates[2].entity_name == "미상"
    assert result.candidates[3].entity_name == "비요른"
    assert result.fallback_call_count == 1
    assert result.fallback_resolved_count == 1
    assert result.fallback_unresolved_count == 2


def test_resolve_candidates_uses_fallback_when_raw_mention_is_descriptive(
    tmp_path: Path,
) -> None:
    # 실제 문제 사례처럼 raw가 "내려다 본 손"이어도 entity_name이 미상이면 fallback한다.
    llm_client = FakeSubjectResolutionClient(
        response_text="""
        {
          "resolutions": [
            {
              "candidate_id": "candidate-0",
              "resolved_entity_name": "비요른 얀델",
              "reason": "앞뒤 서술 흐름의 주체가 비요른 얀델이다."
            }
          ]
        }
        """
    )
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )

    result = _resolve(
        resolver,
        context=_context(),
        candidates=[
            _candidate(
                entity_name="미상",
                raw_entity_mention="내려다 본 손",
                attribute_name="status.블랙아웃",
            )
        ],
        known_characters=[KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")],
    )

    assert llm_client.call_count == 1
    assert '"raw_entity_mention": "내려다 본 손"' in llm_client.last_user_prompt
    assert result.candidates[0].entity_name == "비요른 얀델"
    assert result.fallback_resolved_count == 1
    assert result.fallback_unresolved_count == 0


def test_resolve_candidates_uses_fallback_for_particle_attached_reference(
    tmp_path: Path,
) -> None:
    # 프롬프트를 어겨 entity_name에 조사가 붙은 지칭어가 와도 fallback 대상으로 잡는다.
    resolver = CharacterSubjectResolver(
        llm_client=FakeSubjectResolutionClient(
            response_text="""
            {
              "resolutions": [
                {
                  "candidate_id": "candidate-0",
                  "resolved_entity_name": "주인공은",
                  "reason": "구체 이름 대신 지칭어만 확인했다."
                }
              ]
            }
            """
        ),
        prompt_path=_prompt_path(tmp_path),
    )

    result = _resolve(
        resolver,
        context=_context(),
        candidates=[
            _candidate(
                entity_name="주인공에게는",
                raw_entity_mention=None,
                attribute_name="status.부상",
            )
        ],
        known_characters=[KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")],
    )

    assert result.candidates[0].entity_name == "미상"
    assert result.fallback_call_count == 1
    assert result.fallback_unresolved_count == 1


@pytest.mark.parametrize(
    "response_text",
    [
        '{"resolutions":[]}',
        """
        {
          "resolutions": [
            {"candidate_id":"candidate-0","resolved_entity_name":null},
            {"candidate_id":"candidate-0","resolved_entity_name":"비요른 얀델"}
          ]
        }
        """,
        """
        {
          "resolutions": [
            {"candidate_id":"candidate-1","resolved_entity_name":"비요른 얀델"}
          ]
        }
        """,
    ],
)
def test_resolve_candidates_rejects_mismatched_response_candidate_ids(
    tmp_path: Path,
    response_text: str,
) -> None:
    # 누락·중복·예상 밖 ID는 판단 실패가 아니라 LLM 응답 계약 위반이다.
    resolver = CharacterSubjectResolver(
        llm_client=FakeSubjectResolutionClient(response_text=response_text),
        prompt_path=_prompt_path(tmp_path),
    )

    with pytest.raises(LlmExtractionError, match="candidate IDs"):
        _resolve(
            resolver,
            context=_context(),
            candidates=[_candidate(entity_name="미상", raw_entity_mention="나")],
            known_characters=[KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")],
        )


@pytest.mark.parametrize(
    "raw_mention", ["나는", "그녀는", "그 소년", "저 병사는", None, "미상", "unknown", "없음"]
)
def test_resolve_candidates_rechecks_concrete_draft_name_for_contextual_reference(
    tmp_path: Path,
    raw_mention: str | None,
) -> None:
    # 기존 이름을 잘못 고른 초안도 원문에 이름이 없으면 한 번 독립 해소한다.
    llm_client = FakeSubjectResolutionClient(
        response_text=json.dumps({
            "resolutions": [{
                "candidate_id": "candidate-0",
                "resolved_entity_name": "비요른 얀델",
            }]
        })
    )
    resolver = CharacterSubjectResolver(llm_client=llm_client, prompt_path=_prompt_path(tmp_path))
    candidate = _candidate(entity_name="아이나르", raw_entity_mention=raw_mention)

    result = _resolve(
        resolver,
        context=_context(),
        candidates=[candidate],
        known_characters=[
            KnownCharacter(character_id=AINAR_ID, name="아이나르"),
            KnownCharacter(character_id=BJORN_ID, name="비요른 얀델"),
        ],
    )

    assert llm_client.call_count == 1
    assert result.candidates[0].entity_name == "비요른 얀델"
    assert candidate.entity_name == "아이나르"
    assert result.fallback_resolved_count == 1
    name_match = resolve_candidate_character(
        result.candidates[0],
        normalize_known_characters([KnownCharacter(character_id=BJORN_ID, name="비요른 얀델")]),
    )
    assert name_match.matched_character_id == BJORN_ID


@pytest.mark.parametrize("resolved_name", [None, "그 병사", "그 소년은", "unknown"])
def test_resolve_candidates_leaves_known_draft_unknown_without_subject_evidence(
    tmp_path: Path,
    resolved_name: str | None,
) -> None:
    llm_client = FakeSubjectResolutionClient(
        response_text=json.dumps({
            "resolutions": [{
                "candidate_id": "candidate-0",
                "resolved_entity_name": resolved_name,
            }]
        })
    )
    resolver = CharacterSubjectResolver(llm_client=llm_client, prompt_path=_prompt_path(tmp_path))
    known_characters = [KnownCharacter(character_id=AINAR_ID, name="아이나르")]

    result = _resolve(
        resolver,
        context=SubjectResolutionChunkContext(None, "그녀는 검을 뽑았다.", None),
        candidates=[_candidate(entity_name="아이나르", raw_entity_mention="그녀")],
        known_characters=known_characters,
    )

    assert result.candidates[0].entity_name == "미상"
    assert result.fallback_unresolved_count == 1
    name_match = resolve_candidate_character(
        result.candidates[0], normalize_known_characters(known_characters)
    )
    assert name_match.match_status.value == "AMBIGUOUS"
    assert name_match.matched_character_id is None


def test_resolve_candidates_passes_prior_episode_as_subject_context_without_changing_evidence() -> None:
    # 실제 prompt 계약도 함께 확인한다. 응답에 섞인 값/근거 수정은 반영하지 않는다.
    llm_client = FakeSubjectResolutionClient(
        response_text=json.dumps({
            "resolutions": [{
                "candidate_id": "candidate-0",
                "resolved_entity_name": "비요른 얀델",
                "attribute_value": "999",
                "evidence_quotes": ["이전 회차에서 온 근거"],
            }]
        })
    )
    resolver = CharacterSubjectResolver(llm_client=llm_client)
    candidate = _candidate(entity_name="아이나르", raw_entity_mention="나는").model_copy(
        update={"evidence_spans": [ExtractedEvidenceSpan(quote="나는 멈췄다.", start_offset=21, end_offset=28)]}
    )
    original_payload = candidate.model_dump()
    prior_episode = "비요른 얀델은 자신을 소개한 뒤 동굴로 들어갔다."
    context = SubjectResolutionChunkContext(
        previous_chunk_text=None,
        current_chunk_text="나는 멈췄다.",
        next_chunk_text=None,
        previous_episode_text=prior_episode,
    )

    result = _resolve(
        resolver,
        context=context,
        candidates=[candidate],
        known_characters=[
            KnownCharacter(character_id=AINAR_ID, name="아이나르"),
            KnownCharacter(character_id=BJORN_ID, name="비요른 얀델"),
        ],
    )

    prompt_payload = json.loads(llm_client.last_user_prompt.split("\n\n", 1)[1])
    assert prompt_payload["known_characters"] == ["아이나르", "비요른 얀델"]
    assert str(BJORN_ID) not in llm_client.last_user_prompt
    assert str(AINAR_ID) not in llm_client.last_user_prompt
    assert prompt_payload["context"]["previous_episode"] == prior_episode
    assert prompt_payload["candidates"][0]["draft_entity_name"] == "아이나르"
    assert prompt_payload["candidates"][0]["evidence_quotes"] == ["나는 멈췄다."]
    assert result.candidates[0].model_dump(exclude={"entity_name"}) == candidate.model_dump(exclude={"entity_name"})
    assert candidate.model_dump() == original_payload
    assert "미검증 초안" in llm_client.last_system_prompt
    assert "current_chunk의 시점 전환" in llm_client.last_system_prompt
    assert "이전 회차의 설정 값이나 문장을 현재 후보의 값·근거로 가져오지 않습니다" in llm_client.last_system_prompt


def test_resolve_candidates_checks_new_setting_name_but_preserves_discovery(
    tmp_path: Path,
) -> None:
    llm_client = FakeSubjectResolutionClient(response_text=json.dumps({
        "resolutions": [{
            "candidate_id": "candidate-0", "resolved_entity_name": "리안",
            "reason": "원문에서 리안이라는 새 인물임이 확인됨",
        }],
    }))
    resolver = CharacterSubjectResolver(llm_client=llm_client, prompt_path=_prompt_path(tmp_path))
    explicit_setting = _candidate(entity_name="리안", raw_entity_mention="그 병사 리안")
    discovery = ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        candidate_kind="CHARACTER_DISCOVERY",
        entity_type="CHARACTER",
        entity_name="세룸",
        raw_entity_mention="그 소년",
        attribute_name=None,
        attribute_value=None,
        value_type=None,
        value_json=None,
        evidence_spans=[ExtractedEvidenceSpan(quote="세룸이라는 소년이 들어왔다.")],
        confidence=0.95,
    )

    result = _resolve(
        resolver,
        context=_context(),
        candidates=[explicit_setting, discovery],
        known_characters=[KnownCharacter(character_id=AINAR_ID, name="아이나르")],
    )

    assert llm_client.call_count == 1
    assert result.candidates == [explicit_setting, discovery]


def test_resolve_candidates_checks_unregistered_role_and_preserves_source(tmp_path: Path) -> None:
    llm_client = FakeSubjectResolutionClient(response_text=json.dumps({
        "resolutions": [{
            "candidate_id": "candidate-0", "resolved_entity_name": "리안",
            "reason": "장면에서 그 병사가 리안이라는 사실이 확인됨",
        }],
    }))
    resolver = CharacterSubjectResolver(llm_client=llm_client, prompt_path=_prompt_path(tmp_path))
    candidate = _candidate(entity_name="병사", raw_entity_mention="병사")
    before = candidate.model_dump(mode="json")
    result = _resolve(
        resolver, context=SubjectResolutionChunkContext(
            previous_chunk_text=None, current_chunk_text="리안은 병사였다. 그 병사는 팔을 다쳤다.",
            next_chunk_text=None,
        ), candidates=[candidate],
        known_characters=[KnownCharacter(character_id=AINAR_ID, name="리안")],
    )
    assert llm_client.call_count == 1
    assert result.candidates[0].model_dump(mode="json") == {**before, "entity_name": "리안"}
    assert candidate.model_dump(mode="json") == before


@pytest.mark.parametrize("registered_name", [None, "미라"])
@pytest.mark.parametrize("raw_mention", ["리안", "그는"])
def test_resolve_candidates_accepts_source_grounded_new_name_without_prior_episode(
    registered_name: str | None,
    raw_mention: str,
) -> None:
    # 알려진 목록이 없거나 다른 사람만 등록되어도 신규 주체의 원문 해소는 가능하다.
    # 실제 모델의 의미 정확도가 아니라 입력 계약과 해소 결과 적용 경계를 검증한다.
    llm_client = FakeSubjectResolutionClient(response_text=json.dumps({
        "resolutions": [{
            "candidate_id": "candidate-0",
            "resolved_entity_name": "리안",
            "reason": "리안이라는 자기소개 뒤 같은 인물의 레벨이 제시된다.",
        }],
    }))
    resolver = CharacterSubjectResolver(llm_client=llm_client)
    source = "새 전사가 자신을 리안이라고 소개했다. 그는 레벨이 1이었다."
    quote = "그는 레벨이 1이었다."
    candidate = _candidate(entity_name="리안", raw_entity_mention=raw_mention).model_copy(
        update={"evidence_spans": [ExtractedEvidenceSpan(
            quote=quote,
            start_offset=source.index(quote),
            end_offset=len(source),
        )]}
    )
    before = candidate.model_dump(mode="json")
    known_characters = (
        [KnownCharacter(character_id=AINAR_ID, name=registered_name)]
        if registered_name is not None else []
    )

    result = _resolve(
        resolver,
        context=SubjectResolutionChunkContext(None, source, None),
        candidates=[candidate],
        known_characters=known_characters,
    )

    prompt_payload = json.loads(llm_client.last_user_prompt.split("\n\n", 1)[1])
    assert prompt_payload["known_characters"] == ([registered_name] if registered_name else [])
    assert prompt_payload["context"]["previous_episode"] is None
    assert prompt_payload["context"]["current_chunk"] == source
    assert "반환할 이름의 허용 목록이 아닙니다" in llm_client.last_system_prompt
    assert "신규 인물의 이름이나 유일한 고유 호칭을 반환할 수 있습니다" in llm_client.last_system_prompt
    assert "현재 청크에서 주체가 명확하면 이전 회차의 근거를 추가로 요구하지 않습니다" in llm_client.last_system_prompt
    assert "known_characters 이름 목록 안에서만" not in llm_client.last_system_prompt
    assert llm_client.call_count == result.fallback_call_count == 1
    assert result.fallback_resolved_count == 1
    assert result.fallback_unresolved_count == 0
    assert result.candidates[0].model_dump(mode="json") == before
    assert candidate.model_dump(mode="json") == before


def test_resolve_candidates_does_not_restore_new_draft_after_null_response() -> None:
    # 모델이 null을 반환한 경우 목록이 비었다는 이유로 초안을 자동 승인하지 않는다.
    llm_client = FakeSubjectResolutionClient(response_text=json.dumps({
        "resolutions": [{
            "candidate_id": "candidate-0",
            "resolved_entity_name": None,
            "reason": "누구의 레벨인지 원문으로 특정할 수 없다.",
        }],
    }))
    resolver = CharacterSubjectResolver(llm_client=llm_client)
    candidate = _candidate(entity_name="리안", raw_entity_mention="그는")
    before = candidate.model_dump(mode="json")

    result = _resolve(
        resolver,
        context=SubjectResolutionChunkContext(None, "두 전사가 있었다. 그는 레벨이 1이었다.", None),
        candidates=[candidate],
        known_characters=[],
    )

    assert result.fallback_resolved_count == 0
    assert result.fallback_unresolved_count == 1
    assert result.candidates[0].model_dump(mode="json") == {**before, "entity_name": "미상"}
    assert candidate.model_dump(mode="json") == before


def _resolve(resolver: CharacterSubjectResolver, **kwargs):
    return asyncio.run(resolver.resolve_candidates(**kwargs))


class FakeSubjectResolutionClient:
    def __init__(self, response_text: str) -> None:
        self.response_text = response_text
        self.call_count = 0
        self.last_system_prompt = ""
        self.last_user_prompt = ""
        self.last_prompt_cache_key = None

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        self.call_count += 1
        self.last_system_prompt = system_prompt
        self.last_user_prompt = user_prompt
        self.last_prompt_cache_key = prompt_cache_key
        assert prompt_cache_key == "subject-resolution:v5"
        return LlmTextResponse(text=self.response_text)


def _context() -> SubjectResolutionChunkContext:
    return SubjectResolutionChunkContext(
        previous_chunk_text="비요른 얀델은 던전 입구에서 도끼를 점검했다.",
        current_chunk_text="나는 더 이상 물러설 수 없었다. 그녀는 검을 뽑았다.",
        next_chunk_text="비요른은 괴물의 팔을 피했고, 아이나르는 뒤를 엄호했다.",
    )


def _candidate(
    entity_name: str,
    raw_entity_mention: str | None,
    attribute_name: str = "level",
) -> ExtractedSettingCandidate:
    return ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID,
        entity_type="CHARACTER",
        entity_name=entity_name,
        raw_entity_mention=raw_entity_mention,
        attribute_name=attribute_name,
        attribute_value="1",
        value_type="NUMBER",
        value_json={"value": 1},
        evidence_spans=[
            ExtractedEvidenceSpan(
                quote="나는 더 이상 물러설 수 없었다.",
                start_offset=None,
                end_offset=None,
            )
        ],
        confidence=0.9,
    )


def _prompt_path(tmp_path: Path) -> Path:
    prompt_path = tmp_path / "subject_prompt.md"
    prompt_path.write_text("주체만 해소하고 JSON만 반환하세요.", encoding="utf-8")
    return prompt_path
