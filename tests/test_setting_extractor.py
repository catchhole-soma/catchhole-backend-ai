from uuid import UUID

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
