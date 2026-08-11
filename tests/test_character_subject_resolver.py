import asyncio
from pathlib import Path
from uuid import UUID

import pytest

from app.analysis.character_name_resolver import (
    KnownCharacter,
    normalize_known_characters,
    resolve_candidate_character,
)
from app.analysis.exceptions import LlmExtractionError
from app.analysis.character_subject_resolver import (
    CharacterSubjectResolver,
    SubjectResolutionChunkContext,
)
from app.analysis.schemas import ExtractedEvidenceSpan, ExtractedSettingCandidate
from app.llm.responses import LlmTextResponse

BJORN_ID = UUID("00000000-0000-0000-0000-000000000101")
AINAR_ID = UUID("00000000-0000-0000-0000-000000000102")
CHUNK_ID = UUID("00000000-0000-0000-0000-000000000201")


def test_resolve_candidates_skips_llm_when_fallback_targets_do_not_exist(tmp_path: Path) -> None:
    # entity_name이 모두 구체 이름이면 subject resolver LLM을 추가 호출하지 않는다.
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


def test_resolve_candidates_treats_exact_known_name_as_concrete_before_particle_check(
    tmp_path: Path,
) -> None:
    # "나은"의 "은"을 조사로 제거해 "나"라는 지칭어로 오인하지 않는다.
    llm_client = FakeSubjectResolutionClient(response_text='{"resolutions":[]}')
    resolver = CharacterSubjectResolver(
        llm_client=llm_client,
        prompt_path=_prompt_path(tmp_path),
    )
    candidates = [_candidate(entity_name="나은", raw_entity_mention="나은")]

    result = _resolve(
        resolver,
        context=_context(),
        candidates=candidates,
        known_characters=[KnownCharacter(character_id=AINAR_ID, name="나은")],
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
    assert result.fallback_resolved_count == 0
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
        assert prompt_cache_key == "subject-resolution:v1"
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
