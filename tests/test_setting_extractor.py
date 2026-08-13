import asyncio
from uuid import UUID

import pytest

from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.exceptions import LlmExtractionError
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.llm.responses import LlmTextResponse

CHUNK_ID = UUID("00000000-0000-0000-0000-000000000001")
DEFAULT_SCHEMA_HINTS = (
    CharacterSettingSchemaHint(
        schema_key="level",
        display_name="레벨",
        attribute_pattern=None,
        aliases=("레벨", "level"),
        value_type="NUMBER",
    ),
)


def test_extract_from_chunk_parses_llm_json_result(tmp_path) -> None:
    # LLM의 source_chunk_id가 잘못되어도 Worker 입력 ID로 보정해 검증된 후보를 돌려준다.
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("JSON만 반환하세요.", encoding="utf-8")
    extractor = CharacterSettingExtractor(
        llm_client=FakeTextGenerationClient(),
        prompt_path=prompt_path,
    )

    result = _extract(
        extractor,
        source_chunk_id=CHUNK_ID,
        chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
        episode_no=3,
        episode_title="사라진 이름",
        schema_hints=DEFAULT_SCHEMA_HINTS,
    )

    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.source_chunk_id == CHUNK_ID
    assert candidate.entity_type == "CHARACTER"
    assert candidate.entity_name == "카엘"
    assert candidate.attribute_name == "level"
    assert candidate.attribute_value == "12"
    assert candidate.value_type == "NUMBER"
    assert candidate.value_json == {"value": 12}
    assert candidate.evidence_spans[0].quote == "카엘은 12레벨 검사"


def test_extract_from_chunk_parses_character_discovery_and_family_setting(
    tmp_path,
) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("캐릭터 발견과 설정을 JSON으로 반환하세요.", encoding="utf-8")
    extractor = CharacterSettingExtractor(
        llm_client=CharacterDiscoveryTextGenerationClient(),
        prompt_path=prompt_path,
        max_attempts=1,
    )

    result = _extract(
        extractor,
        source_chunk_id=CHUNK_ID,
        chunk_text="케닉의 넷째 아들 세룸은 나와라!",
        schema_hints=(
            CharacterSettingSchemaHint(
                schema_key="profile.family_relation",
                display_name="가족 관계",
                attribute_pattern=None,
                aliases=("가족 관계",),
                value_type="STRING",
            ),
        ),
        known_characters=(
            KnownCharacter(
                character_id=UUID("00000000-0000-0000-0000-000000000002"),
                name="케닉",
            ),
        ),
    )

    assert len(result.candidates) == 2
    discovery, family_setting = result.candidates
    assert discovery.candidate_kind == "CHARACTER_DISCOVERY"
    assert discovery.entity_name == "세룸"
    assert discovery.raw_entity_mention == "케닉의 넷째 아들 세룸"
    assert discovery.attribute_name is None
    assert discovery.attribute_value is None
    assert discovery.value_type is None
    assert discovery.value_json is None
    assert family_setting.candidate_kind == "SETTING"
    assert family_setting.entity_name == "세룸"
    assert family_setting.attribute_name == "profile.family_relation"
    assert family_setting.value_json == {"value": "케닉의 넷째 아들"}


def test_extract_from_chunk_includes_schema_hints_and_matching_rules_in_prompts() -> None:
    llm_client = RecordingTextGenerationClient()
    extractor = CharacterSettingExtractor(
        llm_client=llm_client,
        max_attempts=1,
    )

    result = _extract(
        extractor,
        source_chunk_id=CHUNK_ID,
        chunk_text="카엘은 화염검술을 익혔고 지능은 17이다.",
        schema_hints=(
            CharacterSettingSchemaHint(
                schema_key="stats.mental_power",
                display_name="정신력",
                attribute_pattern=None,
                aliases=("정신력", "mental_power"),
                value_type="NUMBER",
            ),
            CharacterSettingSchemaHint(
                schema_key="skills.skill",
                display_name="스킬",
                attribute_pattern="skill.*",
                aliases=(),
                value_type="JSON",
            ),
            CharacterSettingSchemaHint(
                schema_key="profile.species",
                display_name="종족",
                attribute_pattern=None,
                aliases=("종족", "species", "race"),
                value_type="STRING",
            ),
        ),
        known_characters=(
            KnownCharacter(
                character_id=UUID("00000000-0000-0000-0000-000000000002"),
                name="비요른 얀델",
            ),
        ),
    )

    assert result.candidates == []
    assert '"schemaKey": "stats.mental_power"' in llm_client.user_prompt
    assert '"displayName": "정신력"' in llm_client.user_prompt
    assert '"aliases": [' in llm_client.user_prompt
    assert '"mental_power"' in llm_client.user_prompt
    assert '"attributePattern": "skill.*"' in llm_client.user_prompt
    assert '"valueType": "JSON"' in llm_client.user_prompt
    assert '"schemaKey": "profile.species"' in llm_client.user_prompt
    assert '"valueType": "STRING"' in llm_client.user_prompt
    assert 'known_character_names:\n["비요른 얀델"]' in llm_client.user_prompt
    assert "canonical schemaKey" in llm_client.user_prompt
    assert "schemaKey, displayName, aliases 또는 attributePattern" in llm_client.user_prompt
    assert "후보에서 제외" in llm_client.user_prompt
    assert "time.첫전투" not in llm_client.user_prompt
    assert "profile.<프로필명>" in llm_client.system_prompt
    assert "subject resolver용 임시값 `미상`" in llm_client.system_prompt
    assert "설정의 주체를 가리키는 최소 표현" in llm_client.system_prompt
    assert "실제 캐릭터명이 명확히 연결되면 반드시 해당 이름" in llm_client.system_prompt
    assert "현재 청크만으로 한 캐릭터를 유일하게 특정할 수 없을 때만" in llm_client.system_prompt
    assert "설정의 주체 자체를 가리키는 최소 표현만 사용합니다" in llm_client.system_prompt
    assert "`나`, `그`, `그녀`, `주인공` 같은 지칭어를 넣지 않습니다" in (
        llm_client.system_prompt
    )
    assert "타임라인에 해당하는 정보는 현재 추출하지 않습니다" in llm_client.system_prompt
    assert "별도 설정이 없더라도 원문에서 이름이 명시된 신규 캐릭터" in (
        llm_client.system_prompt
    )
    assert "`candidate_kind`를 `CHARACTER_DISCOVERY`" in llm_client.system_prompt
    assert "출생 순서, 가족 관계, 서열은 `age`가 아니며" in llm_client.system_prompt
    assert "동일한 캐릭터, 동일한 `attribute_name`, 동일한 `value_type`" in (
        llm_client.system_prompt
    )
    assert "실제 설정값이 달라졌다면 서로 다른 후보로 유지합니다" in (
        llm_client.system_prompt
    )
    assert "완화·종료·다른 상태로 전환된 현재 결과" in llm_client.system_prompt
    assert "능력 회복·증상 소멸·행동 변화·외부 효과 해제" in llm_client.system_prompt
    assert "과거에 상태가 있었다고 역으로 만들어 내지 않습니다" in llm_client.system_prompt
    assert (
        "`schemaKey`, `displayName`, `aliases` 또는 `attributePattern`"
        in llm_client.system_prompt
    )
    assert "time.<시간 또는 사건명>" not in llm_client.system_prompt
    assert "skill.<스킬명>" in llm_client.system_prompt
    assert "item.<아이템명>" in llm_client.system_prompt
    assert llm_client.prompt_cache_key is not None


def test_extract_from_chunk_canonicalizes_schema_order_for_prompt_cache() -> None:
    first_client = RecordingTextGenerationClient()
    second_client = RecordingTextGenerationClient()
    first_schema = CharacterSettingSchemaHint(
        schema_key="profile.species",
        display_name="종족",
        attribute_pattern=None,
        aliases=("species", "종족"),
        value_type="STRING",
    )
    second_schema = CharacterSettingSchemaHint(
        schema_key="stats.strength",
        display_name="근력",
        attribute_pattern=None,
        aliases=("strength", "근력"),
        value_type="NUMBER",
    )
    work_schema = CharacterSettingSchemaHint(
        schema_key="stats.strength",
        display_name="작품 근력",
        attribute_pattern="stats.*",
        aliases=("power", "작품 근력"),
        value_type="NUMBER",
    )

    _extract(
        CharacterSettingExtractor(llm_client=first_client, max_attempts=1),
        source_chunk_id=CHUNK_ID,
        chunk_text="카엘은 인간이며 근력은 10이다.",
        schema_hints=(first_schema, second_schema, work_schema),
    )
    _extract(
        CharacterSettingExtractor(llm_client=second_client, max_attempts=1),
        source_chunk_id=CHUNK_ID,
        chunk_text="카엘은 인간이며 근력은 10이다.",
        schema_hints=(
            CharacterSettingSchemaHint(
                schema_key=work_schema.schema_key,
                display_name=work_schema.display_name,
                attribute_pattern=work_schema.attribute_pattern,
                aliases=tuple(reversed(work_schema.aliases)),
                value_type=work_schema.value_type,
            ),
            second_schema,
            CharacterSettingSchemaHint(
                schema_key=first_schema.schema_key,
                display_name=first_schema.display_name,
                attribute_pattern=first_schema.attribute_pattern,
                aliases=tuple(reversed(first_schema.aliases)),
                value_type=first_schema.value_type,
            ),
        ),
    )

    assert first_client.user_prompt == second_client.user_prompt
    assert first_client.prompt_cache_key == second_client.prompt_cache_key
    assert first_client.user_prompt.index('"profile.species"') < first_client.user_prompt.index(
        '"stats.strength"'
    )


def test_extract_from_chunk_rejects_empty_schema_hints_before_llm_call() -> None:
    llm_client = RecordingTextGenerationClient()
    extractor = CharacterSettingExtractor(
        llm_client=llm_client,
        max_attempts=1,
    )

    with pytest.raises(
        ValueError,
        match="schema_hints must include at least one character setting schema",
    ):
        _extract(
            extractor,
            source_chunk_id=CHUNK_ID,
            chunk_text="카엘은 12레벨 검사였다.",
        )

    assert llm_client.call_count == 0


def test_extract_from_chunk_retries_when_json_parse_fails(tmp_path) -> None:
    # 첫 응답이 JSON이 아니어도 다음 응답이 정상이면 추출이 성공해야 한다.
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("JSON만 반환하세요.", encoding="utf-8")
    llm_client = RetryThenSuccessClient()
    extractor = CharacterSettingExtractor(
        llm_client=llm_client,
        prompt_path=prompt_path,
        max_attempts=2,
    )

    result = _extract(
        extractor,
        source_chunk_id=CHUNK_ID,
        chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
        schema_hints=DEFAULT_SCHEMA_HINTS,
    )

    assert llm_client.call_count == 2
    assert result.candidates[0].entity_name == "카엘"


def test_extract_from_chunk_retries_when_required_field_is_missing(tmp_path) -> None:
    # JSON 문법은 맞아도 필수 필드가 빠지면 schema 검증 실패로 보고 재시도한다.
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("JSON만 반환하세요.", encoding="utf-8")
    llm_client = MissingFieldThenSuccessClient()
    extractor = CharacterSettingExtractor(
        llm_client=llm_client,
        prompt_path=prompt_path,
        max_attempts=2,
    )

    result = _extract(
        extractor,
        source_chunk_id=CHUNK_ID,
        chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
        schema_hints=DEFAULT_SCHEMA_HINTS,
    )

    assert llm_client.call_count == 2
    assert result.candidates[0].source_chunk_id == CHUNK_ID


def test_extract_from_chunk_retries_when_entity_name_is_whitespace_only(tmp_path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("JSON만 반환하세요.", encoding="utf-8")
    llm_client = WhitespaceEntityNameThenSuccessClient()
    extractor = CharacterSettingExtractor(
        llm_client=llm_client,
        prompt_path=prompt_path,
        max_attempts=2,
    )

    result = _extract(
        extractor,
        source_chunk_id=CHUNK_ID,
        chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
        schema_hints=DEFAULT_SCHEMA_HINTS,
    )

    assert llm_client.call_count == 2
    assert result.candidates[0].entity_name == "카엘"


def test_extract_from_chunk_raises_error_when_required_field_keeps_missing(tmp_path) -> None:
    # 필수 필드 누락이 계속되면 후보 저장 단계로 넘기지 않고 최종 실패 처리한다.
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("JSON만 반환하세요.", encoding="utf-8")
    llm_client = AlwaysMissingFieldClient()
    extractor = CharacterSettingExtractor(
        llm_client=llm_client,
        prompt_path=prompt_path,
        max_attempts=2,
    )

    with pytest.raises(LlmExtractionError):
        _extract(
            extractor,
            source_chunk_id=CHUNK_ID,
            chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
            schema_hints=DEFAULT_SCHEMA_HINTS,
        )

    assert llm_client.call_count == 2


def test_extract_from_chunk_raises_error_after_max_attempts(tmp_path) -> None:
    # 모든 시도가 실패하면 잘못된 응답을 저장 단계로 넘기지 않고 전용 예외를 던진다.
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("JSON만 반환하세요.", encoding="utf-8")
    llm_client = AlwaysInvalidJsonClient()
    extractor = CharacterSettingExtractor(
        llm_client=llm_client,
        prompt_path=prompt_path,
        max_attempts=2,
    )

    with pytest.raises(LlmExtractionError):
        _extract(
            extractor,
            source_chunk_id=CHUNK_ID,
            chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
            schema_hints=DEFAULT_SCHEMA_HINTS,
        )

    assert llm_client.call_count == 2


def _extract(extractor: CharacterSettingExtractor, **kwargs):
    return asyncio.run(extractor.extract_from_chunk(**kwargs))


class FakeTextGenerationClient:
    # 정상 LLM 응답을 흉내 내는 기본 fake client
    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        # source_chunk_id는 LLM이 만들 값이 아니므로 prompt에 노출하지 않는다.
        assert "JSON만 반환하세요." in system_prompt
        assert str(CHUNK_ID) not in user_prompt
        assert max_output_tokens == 4000
        assert prompt_cache_key is not None
        assert prompt_cache_key.startswith("setting-extraction:v5:")
        return LlmTextResponse(
            text="""
            {
              "candidates": [
                {
                  "source_chunk_id": "c80fc205-7fdd-8ab0-8b351745a174",
                  "entity_type": "CHARACTER",
                  "entity_name": "카엘",
                  "attribute_name": "level",
                  "attribute_value": "12",
                  "value_type": "NUMBER",
                  "value_json": {"value": 12},
                  "evidence_spans": [
                    {
                      "quote": "카엘은 12레벨 검사",
                      "start_offset": null,
                      "end_offset": null
                    }
                  ],
                  "confidence": 0.9
                }
              ]
            }
            """
        )


class RecordingTextGenerationClient:
    def __init__(self) -> None:
        self.system_prompt = ""
        self.user_prompt = ""
        self.prompt_cache_key = None
        self.call_count = 0

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        self.call_count += 1
        self.system_prompt = system_prompt
        self.user_prompt = user_prompt
        self.prompt_cache_key = prompt_cache_key
        return LlmTextResponse(text='{"candidates": []}')


class CharacterDiscoveryTextGenerationClient:
    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        assert 'known_character_names:\n["케닉"]' in user_prompt
        return LlmTextResponse(
            text="""
            {
              "candidates": [
                {
                  "candidate_kind": "CHARACTER_DISCOVERY",
                  "entity_type": "CHARACTER",
                  "entity_name": "세룸",
                  "raw_entity_mention": "케닉의 넷째 아들 세룸",
                  "attribute_name": null,
                  "attribute_value": null,
                  "value_type": null,
                  "value_json": null,
                  "evidence_spans": [
                    {
                      "quote": "케닉의 넷째 아들 세룸은 나와라!",
                      "start_offset": null,
                      "end_offset": null
                    }
                  ],
                  "confidence": 0.95
                },
                {
                  "candidate_kind": "SETTING",
                  "entity_type": "CHARACTER",
                  "entity_name": "세룸",
                  "raw_entity_mention": "케닉의 넷째 아들 세룸",
                  "attribute_name": "profile.family_relation",
                  "attribute_value": "케닉의 넷째 아들",
                  "value_type": "STRING",
                  "value_json": {"value": "케닉의 넷째 아들"},
                  "evidence_spans": [
                    {
                      "quote": "케닉의 넷째 아들 세룸은 나와라!",
                      "start_offset": null,
                      "end_offset": null
                    }
                  ],
                  "confidence": 0.9
                }
              ]
            }
            """
        )


class RetryThenSuccessClient:
    # 첫 호출만 깨진 응답을 주고, 두 번째 호출부터 정상 JSON을 주는 fake client
    def __init__(self) -> None:
        self.call_count = 0

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        self.call_count += 1
        if self.call_count == 1:
            return LlmTextResponse(text="이 응답은 JSON이 아닙니다.")
        return await FakeTextGenerationClient().create_text_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_output_tokens=max_output_tokens,
            prompt_cache_key=prompt_cache_key,
        )


class MissingFieldThenSuccessClient:
    # 첫 호출만 필수 필드가 빠진 JSON을 주고, 두 번째 호출부터 정상 JSON을 주는 fake client
    def __init__(self) -> None:
        self.call_count = 0

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        self.call_count += 1
        if self.call_count == 1:
            return LlmTextResponse(
                text="""
                {
                  "candidates": [
                    {
                      "source_chunk_id": "00000000-0000-0000-0000-000000000001",
                      "entity_type": "CHARACTER",
                      "entity_name": "카엘",
                      "attribute_name": "level",
                      "attribute_value": "12",
                      "value_json": {"value": 12},
                      "evidence_spans": [
                        {
                          "quote": "카엘은 12레벨 검사",
                          "start_offset": null,
                          "end_offset": null
                        }
                      ],
                      "confidence": 0.9
                    }
                  ]
                }
                """
            )
        return await FakeTextGenerationClient().create_text_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_output_tokens=max_output_tokens,
            prompt_cache_key=prompt_cache_key,
        )


class WhitespaceEntityNameThenSuccessClient:
    def __init__(self) -> None:
        self.call_count = 0

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        self.call_count += 1
        if self.call_count == 1:
            return LlmTextResponse(
                text="""
                {
                  "candidates": [
                    {
                      "source_chunk_id": "00000000-0000-0000-0000-000000000001",
                      "entity_type": "CHARACTER",
                      "entity_name": "   ",
                      "raw_entity_mention": "카엘",
                      "attribute_name": "level",
                      "attribute_value": "12",
                      "value_type": "NUMBER",
                      "value_json": {"value": 12},
                      "evidence_spans": [
                        {
                          "quote": "카엘은 12레벨 검사",
                          "start_offset": null,
                          "end_offset": null
                        }
                      ],
                      "confidence": 0.9
                    }
                  ]
                }
                """
            )
        return await FakeTextGenerationClient().create_text_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_output_tokens=max_output_tokens,
            prompt_cache_key=prompt_cache_key,
        )


class AlwaysInvalidJsonClient:
    # 최대 재시도 이후 실패 흐름을 확인하기 위해 계속 깨진 응답을 주는 fake client
    def __init__(self) -> None:
        self.call_count = 0

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        self.call_count += 1
        return LlmTextResponse(text="이 응답은 끝까지 JSON이 아닙니다.")


class AlwaysMissingFieldClient:
    # 최대 재시도 이후 schema 검증 실패 흐름을 확인하기 위해 계속 필수 필드를 누락한다.
    def __init__(self) -> None:
        self.call_count = 0

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        self.call_count += 1
        return LlmTextResponse(
            text="""
            {
              "candidates": [
                {
                  "source_chunk_id": "00000000-0000-0000-0000-000000000001",
                  "entity_type": "CHARACTER",
                  "entity_name": "카엘",
                  "attribute_name": "level",
                  "attribute_value": "12",
                  "value_json": {"value": 12},
                  "evidence_spans": [
                    {
                      "quote": "카엘은 12레벨 검사",
                      "start_offset": null,
                      "end_offset": null
                    }
                  ],
                  "confidence": 0.9
                }
              ]
            }
            """
        )
