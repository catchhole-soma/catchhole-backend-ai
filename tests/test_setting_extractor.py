from uuid import UUID

import pytest

from app.analysis.exceptions import LlmExtractionError
from app.analysis.setting_extractor import CharacterSettingExtractor
from app.llm.responses import LlmTextResponse

CHUNK_ID = UUID("00000000-0000-0000-0000-000000000001")


def test_extract_from_chunk_parses_llm_json_result(tmp_path) -> None:
    prompt_path = tmp_path / "prompt.md"
    prompt_path.write_text("JSON만 반환하세요.", encoding="utf-8")
    extractor = CharacterSettingExtractor(
        llm_client=FakeTextGenerationClient(),
        prompt_path=prompt_path,
    )

    result = extractor.extract_from_chunk(
        source_chunk_id=CHUNK_ID,
        chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
        episode_no=3,
        episode_title="사라진 이름",
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

    result = extractor.extract_from_chunk(
        source_chunk_id=CHUNK_ID,
        chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
    )

    assert llm_client.call_count == 2
    assert result.candidates[0].entity_name == "카엘"


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
        extractor.extract_from_chunk(
            source_chunk_id=CHUNK_ID,
            chunk_text="카엘은 12레벨 검사로, 화염검을 장비하고 있었다.",
        )

    assert llm_client.call_count == 2


class FakeTextGenerationClient:
    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
    ) -> LlmTextResponse:
        assert "JSON만 반환하세요." in system_prompt
        assert str(CHUNK_ID) in user_prompt
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


class RetryThenSuccessClient:
    # 첫 호출만 깨진 응답을 주고, 두 번째 호출부터 정상 JSON을 주는 fake client
    def __init__(self) -> None:
        self.call_count = 0

    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
    ) -> LlmTextResponse:
        self.call_count += 1
        if self.call_count == 1:
            return LlmTextResponse(text="이 응답은 JSON이 아닙니다.")
        return FakeTextGenerationClient().create_text_response(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            model=model,
            max_output_tokens=max_output_tokens,
        )


class AlwaysInvalidJsonClient:
    # 최대 재시도 이후 실패 흐름을 확인하기 위해 계속 깨진 응답을 주는 fake client
    def __init__(self) -> None:
        self.call_count = 0

    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
    ) -> LlmTextResponse:
        self.call_count += 1
        return LlmTextResponse(text="이 응답은 끝까지 JSON이 아닙니다.")
