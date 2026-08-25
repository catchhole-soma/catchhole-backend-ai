import asyncio
import json
from uuid import UUID

import pytest

from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.analysis.world_setting_schemas import ExtractedWorldSettingCandidate
from app.llm.exceptions import LlmOutputTruncatedError
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

    result = asyncio.run(
        extractor.extract_from_chunk("바바리안은 혹한 지역에서 살아간다.", 1, "서장")
    )

    assert result.candidates[0].category == category
    assert result.candidates[0].confidence == 0.95
    assert "chunk_text" in client.requests[0]["user_prompt"]


def test_world_setting_extractor_retries_confidence_outside_fixed_scale() -> None:
    invalid = _response(category="RACE", confidence=0.7)
    valid = _response(category="RACE", confidence=0.8)
    client = FakeTextClient([invalid, valid])

    result = asyncio.run(
        WorldSettingExtractor(llm_client=client, max_attempts=2).extract_from_chunk(
            "바바리안은 혹한 지역에서 살아간다."
        )
    )

    assert result.candidates[0].confidence == 0.8
    assert len(client.requests) == 2


def test_world_setting_extractor_accepts_empty_result_for_temporary_event() -> None:
    client = FakeTextClient([json.dumps({"candidates": []})])

    result = asyncio.run(
        WorldSettingExtractor(llm_client=client, max_attempts=1).extract_from_chunk(
            "오늘 왕궁에 비가 내렸다."
        )
    )

    assert result.candidates == []


def test_world_setting_extractor_expands_output_cap_once_after_truncation() -> None:
    client = FakeTextClient([
        _truncated_error(5000),
        json.dumps({"candidates": []}),
    ])

    result = asyncio.run(
        WorldSettingExtractor(
            llm_client=client,
            max_attempts=3,
            max_output_tokens=5000,
            truncation_retry_max_output_tokens=10000,
        ).extract_from_chunk("미궁에는 여러 층의 규칙이 존재한다.")
    )

    assert result.candidates == []
    assert [request["max_output_tokens"] for request in client.requests] == [5000, 10000]


def test_world_setting_extractor_stops_after_second_truncation() -> None:
    client = FakeTextClient([
        _truncated_error(5000),
        _truncated_error(10000),
    ])

    with pytest.raises(LlmOutputTruncatedError):
        asyncio.run(
            WorldSettingExtractor(
                llm_client=client,
                max_attempts=3,
                max_output_tokens=5000,
                truncation_retry_max_output_tokens=10000,
            ).extract_from_chunk("미궁에는 여러 층의 규칙이 존재한다.")
        )

    assert [request["max_output_tokens"] for request in client.requests] == [5000, 10000]


def test_world_setting_truncation_expansion_does_not_consume_validation_attempt() -> None:
    client = FakeTextClient([
        _response(category="RACE", confidence=0.7),
        _truncated_error(5000),
        json.dumps({"candidates": []}),
    ])

    result = asyncio.run(
        WorldSettingExtractor(
            llm_client=client,
            max_attempts=2,
            max_output_tokens=5000,
            truncation_retry_max_output_tokens=10000,
        ).extract_from_chunk("미궁에는 여러 층의 규칙이 존재한다.")
    )

    assert result.candidates == []
    assert [request["max_output_tokens"] for request in client.requests] == [
        5000,
        5000,
        10000,
    ]


def test_world_setting_extractor_accepts_one_level_scope() -> None:
    client = FakeTextClient([_response(category="LOCATION", scope_name="1층")])

    result = asyncio.run(
        WorldSettingExtractor(llm_client=client, max_attempts=1).extract_from_chunk(
            "미궁 1층 동쪽에서는 고블린이 출몰한다."
        )
    )

    assert result.candidates[0].scope_name == "1층"


def test_world_setting_mapper_resolves_offsets_and_consolidates_exact_fact() -> None:
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
    consolidated = WorldSettingCandidateMapper.consolidate_by_key([publish_item, publish_item])

    assert len(consolidated) == 1
    assert publish_item.evidence_spans[0].start_offset == 100
    assert publish_item.evidence_spans[0].end_offset == 100 + len(text)
    assert publish_item.raw_extraction_json["subject_name"] == "바바리안"


def test_world_setting_mapper_consolidates_same_key_values_and_keeps_all_evidence() -> None:
    first = _publish_item(
        setting_name="기능",
        extracted_value="공명시킨 메시지 스톤끼리 대화할 수 있다.",
        quote="미리 공명시켜 둔 메시지 스톤끼리 대화를 나눌 수 있게 해 주는 마도구예요.",
        start_offset=10,
    )
    second = _publish_item(
        setting_name=" 기능 ",
        extracted_value="짧게 읊조려 신호를 보낼 수 있다.",
        quote="메시지 스톤. 이를 봄과 동시 짧게 읊조려 신호를 보냈다.",
        start_offset=80,
    )
    radius = _publish_item(
        setting_name="통신 반경",
        extracted_value="약 300m",
        quote="반경은 300m 정도라고 들었고요.",
        start_offset=150,
    )

    consolidated = WorldSettingCandidateMapper.consolidate_by_key([first, second, radius])

    assert len(consolidated) == 2
    function = consolidated[0]
    assert function.setting_name == "기능"
    assert function.extracted_value == (
        "공명시킨 메시지 스톤끼리 대화할 수 있다.\n"
        "짧게 읊조려 신호를 보낼 수 있다."
    )
    assert [span.quote for span in function.evidence_spans] == [
        "미리 공명시켜 둔 메시지 스톤끼리 대화를 나눌 수 있게 해 주는 마도구예요.",
        "메시지 스톤. 이를 봄과 동시 짧게 읊조려 신호를 보냈다.",
    ]
    assert function.raw_extraction_json["sourceValues"] == [
        "공명시킨 메시지 스톤끼리 대화할 수 있다.",
        "짧게 읊조려 신호를 보낼 수 있다.",
    ]


def test_world_setting_mapper_keeps_same_setting_name_separate_across_scopes() -> None:
    first_floor = _publish_item(
        setting_name="방향별 몬스터 출몰 규칙",
        extracted_value="동쪽에서 고블린이 출몰한다.",
        quote="미궁 1층 동쪽에서는 고블린이 출몰한다.",
        start_offset=10,
        scope_name="1층",
    )
    second_floor = _publish_item(
        setting_name="방향별 몬스터 출몰 규칙",
        extracted_value="동쪽에서 오크가 출몰한다.",
        quote="미궁 2층 동쪽에서는 오크가 출몰한다.",
        start_offset=80,
        scope_name="2층",
    )

    consolidated = WorldSettingCandidateMapper.consolidate_by_key([first_floor, second_floor])

    assert [candidate.scope_name for candidate in consolidated] == ["1층", "2층"]
    assert [candidate.evidence_spans[0].quote for candidate in consolidated] == [
        "미궁 1층 동쪽에서는 고블린이 출몰한다.",
        "미궁 2층 동쪽에서는 오크가 출몰한다.",
    ]


def _publish_item(
    setting_name: str,
    extracted_value: str,
    quote: str,
    start_offset: int,
    scope_name: str | None = None,
):
    return WorldSettingCandidateMapper.to_publish_item(
        ExtractedWorldSettingCandidate.model_validate({
            "category": "IMPORTANT_ITEM",
            "subject_name": "메시지 스톤",
            "scope_name": scope_name,
            "setting_name": setting_name,
            "extracted_value": extracted_value,
            "evidence_spans": [{"quote": quote}],
            "confidence": 0.95,
        }),
        EpisodeChunk(
            id=UUID(int=start_offset),
            episode_id=UUID("00000000-0000-0000-0000-000000000002"),
            chunk_index=0,
            chunk_text=quote,
            start_offset=start_offset,
            end_offset=start_offset + len(quote),
            paragraph_start_index=0,
            paragraph_end_index=0,
            metadata_json=None,
            created_at=None,
            updated_at=None,
        ),
    )


def _response(
    category: str,
    confidence: float = 0.95,
    scope_name: str | None = None,
) -> str:
    return json.dumps(
        {
            "candidates": [
                {
                    "category": category,
                    "subject_name": "바바리안",
                    "scope_name": scope_name,
                    "setting_name": "서식지",
                    "extracted_value": "혹한 지역",
                    "evidence_spans": [{"quote": "바바리안은 혹한 지역에서 살아간다."}],
                    "confidence": confidence,
                }
            ]
        },
        ensure_ascii=False,
    )


def _truncated_error(max_output_tokens: int) -> LlmOutputTruncatedError:
    return LlmOutputTruncatedError(
        "output truncated",
        incomplete_reason="max_output_tokens",
        max_output_tokens=max_output_tokens,
        output_token_count=max_output_tokens,
    )


class FakeTextClient:
    def __init__(self, responses: list[str | Exception]) -> None:
        self.responses = responses
        self.requests: list[dict] = []

    async def create_text_response(self, **kwargs) -> LlmTextResponse:
        self.requests.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LlmTextResponse(text=response)
