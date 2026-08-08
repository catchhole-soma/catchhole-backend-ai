import json
from uuid import UUID

import pytest

from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.analysis.world_setting_schemas import ExtractedWorldSettingCandidate
from app.llm.responses import LlmTextResponse
from app.mappers.world_setting_candidate_mapper import WorldSettingCandidateMapper
from app.models.episode_chunk import EpisodeChunk


@pytest.mark.parametrize(
    "category",
    [
        "RACE",
        "FACTION",
        "LOCATION",
        "MONSTER",
        "POWER_SYSTEM",
        "WORLD_RULE_HISTORY",
        "IMPORTANT_ITEM",
    ],
)
def test_world_setting_extractor_accepts_all_categories(category: str) -> None:
    client = FakeTextClient([_response(category=category)])
    extractor = WorldSettingExtractor(llm_client=client, max_attempts=1)

    result = extractor.extract_from_chunk("바바리안은 혹한 지역에서 살아간다.", 1, "서장")

    assert result.candidates[0].category == category
    assert result.candidates[0].confidence == 0.95
    assert "chunk_text" in client.requests[0]["user_prompt"]


def test_world_setting_extractor_retries_confidence_outside_fixed_scale() -> None:
    invalid = _response(category="RACE", confidence=0.7)
    valid = _response(category="RACE", confidence=0.8)
    client = FakeTextClient([invalid, valid])

    result = WorldSettingExtractor(llm_client=client, max_attempts=2).extract_from_chunk(
        "바바리안은 혹한 지역에서 살아간다."
    )

    assert result.candidates[0].confidence == 0.8
    assert len(client.requests) == 2


def test_world_setting_extractor_accepts_empty_result_for_temporary_event() -> None:
    client = FakeTextClient([json.dumps({"candidates": []})])

    result = WorldSettingExtractor(llm_client=client, max_attempts=1).extract_from_chunk(
        "오늘 왕궁에 비가 내렸다."
    )

    assert result.candidates == []


def test_world_setting_mapper_resolves_offsets_and_deduplicates_exact_fact() -> None:
    text = "바바리안은 혹한 지역에서 살아간다."
    chunk = EpisodeChunk(
        id=UUID("00000000-0000-0000-0000-000000000001"),
        episode_id=UUID("00000000-0000-0000-0000-000000000002"),
        chunk_index=0,
        chunk_text=text,
        start_offset=100,
        end_offset=100 + len(text),
        paragraph_start_index=0,
        paragraph_end_index=0,
        metadata_json=None,
        created_at=None,
        updated_at=None,
    )
    candidate = ExtractedWorldSettingCandidate.model_validate(
        json.loads(_response(category="RACE"))["candidates"][0]
    )

    publish_item = WorldSettingCandidateMapper.to_publish_item(candidate, chunk)
    deduplicated = WorldSettingCandidateMapper.deduplicate([publish_item, publish_item])

    assert len(deduplicated) == 1
    assert publish_item.evidence_spans[0].start_offset == 100
    assert publish_item.evidence_spans[0].end_offset == 100 + len(text)
    assert publish_item.raw_extraction_json["subject_name"] == "바바리안"


def _response(category: str, confidence: float = 0.95) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "category": category,
                    "subject_name": "바바리안",
                    "setting_name": "서식지",
                    "extracted_value": "혹한 지역",
                    "evidence_spans": [{"quote": "바바리안은 혹한 지역에서 살아간다."}],
                    "confidence": confidence,
                }
            ]
        },
        ensure_ascii=False,
    )


class FakeTextClient:
    def __init__(self, responses: list[str]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        return LlmTextResponse(text=self.responses.pop(0))
