import asyncio
import inspect
import json
from dataclasses import asdict
from hashlib import sha256
from uuid import UUID

import httpx
import pytest

from app.analysis.exceptions import LlmExtractionError
from app.analysis.relationship_extractor import RELATIONSHIP_RESPONSE_SCHEMA, RelationshipExtractor
from app.analysis.relationship_observations import (
    MAX_RELATIONSHIP_OBSERVATIONS,
    MAX_RELATIONSHIP_SOURCE_SPAN_CHARS,
    ground_relationship_observations,
    relationship_source_spans,
)
from app.analysis.schemas import (
    CharacterSettingExtractionResult,
    character_setting_provider_json_schema,
)
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.responses import LlmTextResponse
from evals.multi_stage_setting.runtime_adapter import create_default_runtime_components
from evals.multi_stage_setting.runtime_cli import _parse_args

CHUNK_ID = UUID(int=1)
QUOTE = "라온과 미루는 함께 출구로 가는 길을 찾았다."
TEXT = QUOTE + "\n라온은 12레벨이다."
HINTS = (CharacterSettingSchemaHint("level", "레벨", None, (), "NUMBER"),)


def observation(**updates):
    return {
        "source": {"name": "라온", "mention": "라온"},
        "target": {"name": "미루", "mention": "미루"},
        "summary": "라온과 미루가 함께 길을 찾는다.",
        "evidence_span_ids": ["S1"],
        **updates,
    }


class Client:
    def __init__(self, payload=None, *, text=None):
        self.calls = []
        self.response = LlmTextResponse(
            text=text if text is not None else json.dumps(payload, ensure_ascii=False),
            input_token_count=100, cached_input_token_count=20, output_token_count=30,
            raw_response={"id": "synthetic-response", "status": "completed"},
        )

    async def create_text_response(self, **kwargs):
        self.calls.append(kwargs)
        return self.response


def extract(client, *, text=TEXT):
    return asyncio.run(RelationshipExtractor(client).extract_from_chunk(
        source_chunk_id=CHUNK_ID, chunk_text=text, episode_no=2, episode_title="합성 장면",
    ))


def test_selected_span_is_reconstructed_exactly_with_usage_and_local_prompt_refs():
    raw = [observation(), {"invalid": "synthetic item"}]
    client = Client({"observations": raw})
    result = extract(client)
    assert len(client.calls) == 1
    assert result.response is client.response
    assert result.raw_observations == raw
    assert [(r.index, r.reason) for r in result.rejections] == [(1, "INVALID_ITEM")]
    value, = result.observations
    span, = value.evidence_spans
    assert TEXT[span.start_offset:span.end_offset] == QUOTE + "\n"
    assert value.evidence_quotes == [span.quote]
    assert value.source_chunk_id == CHUNK_ID
    request, = client.calls
    assert request["model"] == "gpt-6.1-terra"
    assert request["max_output_tokens"] == 16000
    assert request["call_options"].reasoning_effort == "medium"
    assert json.loads(request["user_prompt"]) == {
        "episode_no": 2, "episode_title": "합성 장면",
        "source_spans": [{"ref": "S1", "text": QUOTE + "\n"},
                         {"ref": "S2", "text": "라온은 12레벨이다."}],
    }
    assert str(CHUNK_ID) not in request["user_prompt"]
    assert request["response_schema"] is RELATIONSHIP_RESPONSE_SCHEMA
    fields = RELATIONSHIP_RESPONSE_SCHEMA.schema["$defs"]["RelationshipObservationWire"]["properties"]
    assert "evidence_quotes" not in fields
    assert "evidence_span_ids" in fields


@pytest.mark.parametrize("text", ["", "\n\n", "첫째\r\n둘째\n셋째", "😀e\u0301륽\t" * 600,
                                  "가" * 3000, "앞\n" + "문장. " * 600 + "끝 "])
def test_source_span_partition_is_lossless_bounded_deterministic_unicode(text):
    spans = relationship_source_spans(text)
    assert spans == relationship_source_spans(text)
    assert "".join(span.text for span in spans) == text
    end = 0
    for index, span in enumerate(spans, 1):
        assert span.ref == f"S{index}"
        assert span.start_offset == end
        assert span.text == text[span.start_offset:span.end_offset]
        assert 0 < len(span.text) <= MAX_RELATIONSHIP_SOURCE_SPAN_CHARS
        end = span.end_offset
    assert end == len(text)


def test_repeated_sentences_select_exact_requested_occurrence_without_copying_unicode():
    text = "😀라온과 미루는 륽 소리를 들었다.\n" * 2
    values, rejected = ground_relationship_observations([
        observation(evidence_span_ids=["S2"]),
    ], text, CHUNK_ID)
    assert rejected == []
    span, = values[0].evidence_spans
    assert span.start_offset == len(text) // 2
    assert span.quote == text[span.start_offset:]
    assert "륽" in span.quote


@pytest.mark.parametrize("raw_text", [
    "not json", "[]", "null", "{}", '{"observations": null}',
    '{"observations": {}}', '{"observations": [], "candidates": []}',
    '{"observations": [], "observations": []}',
    '{"observations": [{"source": {"name": "A", "name": "B"}}]}',
])
def test_invalid_envelope_fails_once_without_exposing_provider_text(raw_text):
    client = Client(text=raw_text)
    with pytest.raises(LlmExtractionError, match="^Invalid independent relationship response.$"):
        extract(client)
    assert len(client.calls) == 1


@pytest.mark.parametrize("raw,reason", [
    (None, "MISSING_ARRAY"), ({}, "INVALID_ARRAY"), ([None], "INVALID_ITEM"),
    ([observation(summary={"not": "text"})], "INVALID_ITEM"),
    ([observation(evidence_span_ids=[])], "INVALID_ITEM"),
    ([observation(evidence_span_ids=["S0"])], "INVALID_ITEM"),
    ([observation(evidence_span_ids=["S999"])], "UNKNOWN_SOURCE_SPAN"),
    ([observation(evidence_span_ids=["S1", "S1"])], "INVALID_SPAN_ORDER"),
    ([observation(evidence_span_ids=["S2", "S1"])], "INVALID_SPAN_ORDER"),
    ([observation(evidence_quotes=[QUOTE])], "INVALID_ITEM"),
    ([observation(target={"name": "라온", "mention": "미루"})], "SELF_OR_AMBIGUOUS_ENDPOINTS"),
    ([observation(target={"name": None, "mention": "라온"})], "SELF_OR_AMBIGUOUS_ENDPOINTS"),
    ([observation(target={"name": "다온", "mention": "다온"})], "ENDPOINT_NOT_IN_EVIDENCE"),
    ([observation(target={"name": "다온", "mention": "미루"})], "NAME_NOT_IN_EVIDENCE"),
])
def test_invalid_grounding_records_reason(raw, reason):
    values, rejected = ground_relationship_observations(raw, TEXT, CHUNK_ID)
    assert values == []
    assert [r.reason for r in rejected] == [reason]


def test_total_selected_evidence_cap_is_not_silently_truncated():
    text = ("라온과 미루 " + "가" * 470 + "\n") * 10
    values, rejected = ground_relationship_observations([
        observation(evidence_span_ids=[f"S{i}" for i in range(1, 11)]),
    ], text, CHUNK_ID)
    assert values == []
    assert rejected[0].reason == "EVIDENCE_LIMIT_EXCEEDED"


def test_valid_neighbors_survive_invalid_item_and_duplicate_without_retry():
    result = extract(Client({"observations": [observation(), {}, observation()]}))
    assert len(result.observations) == 1
    assert [(r.index, r.reason) for r in result.rejections] == [(1, "INVALID_ITEM"), (2, "DUPLICATE")]


def test_observation_limit_retains_first_32_and_explicitly_rejects_excess():
    result = extract(Client({"observations": [observation(summary=f"관찰 {i}") for i in range(34)]}))
    assert MAX_RELATIONSHIP_OBSERVATIONS == len(result.observations) == 32
    assert [(r.index, r.reason) for r in result.rejections] == [(32, "LIMIT_EXCEEDED"), (33, "LIMIT_EXCEEDED")]


@pytest.mark.parametrize("text,mention", [
    ("라온에게는 미루가 도움을 주었다.", "라온"),
    ("나는 미루에게 약을 주었다.", "나는"),
    ("Ada helped 미루.", "Ada"),
    ("푸른 모자를 쓴 사람이 미루를 도왔다.", "푸른 모자를 쓴 사람"),
])
def test_mentions_allow_korean_particles_english_and_anonymous_people(text, mention):
    raw = observation(source={"name": None, "mention": mention})
    values, rejected = ground_relationship_observations([raw], text, CHUNK_ID)
    assert rejected == []
    assert values[0].source.name is None


@pytest.mark.parametrize("text,mention", [
    ("새로 태어나는 미루를 라온이 도왔다.", "나는"),
    ("_라온과 미루는 함께 걸었다.", "라온"),
    ("라온7과 미루는 함께 걸었다.", "라온"),
    ("Adaline helped 미루.", "Ada"),
])
def test_word_substrings_do_not_establish_endpoint_mentions(text, mention):
    values, rejected = ground_relationship_observations([
        observation(source={"name": None, "mention": mention}),
    ], text, CHUNK_ID)
    assert values == []
    assert rejected[0].reason == "ENDPOINT_NOT_IN_EVIDENCE"


def test_missing_name_evidence_requires_nullable_name_even_if_elsewhere_in_chunk():
    text = "나는 미루를 도왔다.\n내 이름은 라온이다."
    item = observation(source={"name": "라온", "mention": "나는"})
    assert ground_relationship_observations([item], text, CHUNK_ID)[1][0].reason == "NAME_NOT_IN_EVIDENCE"
    item["evidence_span_ids"] = ["S1", "S2"]
    values, rejected = ground_relationship_observations([item], text, CHUNK_ID)
    assert rejected == []
    assert len(values[0].evidence_spans) == 2


@pytest.mark.parametrize("summary", ["라온만 미루를 친구라고 여겼다.", "과거에 길을 찾았다.",
                                     "함께 길을 찾는다면 좋겠다고 상상했다."])
def test_grounding_is_not_semantic_validation(summary):
    values, rejected = ground_relationship_observations([observation(summary=summary)], TEXT, CHUNK_ID)
    assert rejected == []
    assert values[0].summary == summary


def test_empty_array_is_valid_and_input_budget_fails_before_any_provider_call():
    client = Client({"observations": []})
    assert extract(client).observations == []
    small = Client({"observations": []})
    with pytest.raises(LlmExtractionError, match="token budget"):
        asyncio.run(RelationshipExtractor(small, max_input_tokens=1).extract_from_chunk(
            source_chunk_id=CHUNK_ID, chunk_text=TEXT,
        ))
    assert small.calls == []


@pytest.mark.parametrize("kwargs", [{"model": " "}, {"max_output_tokens": 0},
                                    {"max_output_tokens": 128001}, {"max_output_tokens": True},
                                    {"max_input_tokens": 0}, {"max_input_tokens": True}])
def test_invalid_configuration_does_not_call_provider(kwargs):
    client = Client({"observations": []})
    with pytest.raises(ValueError):
        RelationshipExtractor(client, **kwargs)
    assert client.calls == []


def test_empty_source_and_missing_explicit_client_do_not_use_global_runtime():
    client = Client({"observations": []})
    with pytest.raises(ValueError, match="chunk_text"):
        extract(client, text=" \n")
    with pytest.raises(TypeError):
        RelationshipExtractor()
    assert client.calls == []


def provider_response(text, *, status="completed"):
    return {
        "id": "synthetic-response", "status": status,
        "output": [{"type": "message", "content": [{"type": "output_text", "text": text}]}],
        "usage": {"input_tokens": 100, "output_tokens": 30,
                  "input_tokens_details": {"cached_tokens": 20}},
        **({"incomplete_details": {"reason": "max_output_tokens"}} if status != "completed" else {}),
    }


@pytest.mark.parametrize("outcome", ["success", "http_error", "truncated", "timeout"])
def test_actual_transport_is_one_terra_medium_store_false_request_without_retry(outcome):
    requests = []

    async def run():
        def handler(request):
            requests.append(request)
            if outcome == "timeout":
                raise httpx.ReadTimeout("synthetic timeout", request=request)
            if outcome == "http_error":
                return httpx.Response(429, json={"error": {"message": "synthetic limit"}})
            return httpx.Response(200, json=provider_response(
                json.dumps({"observations": [observation()]}),
                status="incomplete" if outcome == "truncated" else "completed",
            ))

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
            client = OpenAIResponsesClient(
                api_key="synthetic-test-key", model="unused-fallback-model",
                responses_api_url="https://synthetic.invalid/v1/responses",
                reasoning_effort="medium", http_client=http_client,
            )
            return await RelationshipExtractor(client).extract_from_chunk(
                source_chunk_id=CHUNK_ID, chunk_text=TEXT,
            )

    if outcome == "success":
        result = asyncio.run(run())
        assert len(result.observations) == 1
        assert result.response.input_token_count == 100
        assert result.response.cached_input_token_count == 20
        assert result.response.output_token_count == 30
    else:
        expected = {
            "http_error": httpx.HTTPStatusError,
            "truncated": LlmOutputTruncatedError,
            "timeout": httpx.ReadTimeout,
        }[outcome]
        with pytest.raises(expected):
            asyncio.run(run())
    request, = requests
    assert request.url.path == "/v1/responses"
    body = json.loads(request.content)
    assert body["model"] == "gpt-6.1-terra"
    assert body["reasoning"] == {"effort": "medium"}
    assert body["store"] is False
    assert body["max_output_tokens"] == 16000
    assert body["prompt_cache_key"] == "relationship-source-spans:v1"
    assert body["text"]["format"] == {
        "type": "json_schema", "name": "relationship_observations", "strict": True,
        "schema": RELATIONSHIP_RESPONSE_SCHEMA.schema,
    }


def request_hash(request):
    payload = dict(request)
    if payload.get("response_schema") is not None:
        payload["response_schema"] = asdict(payload["response_schema"])
    if payload.get("call_options") is not None:
        payload["call_options"] = asdict(payload["call_options"])
    return sha256(json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    ).encode()).hexdigest()


def test_character_and_world_requests_keep_frozen_original_contract_around_relationship_call():
    # Current main contracts must remain unchanged by creating/running a relation call.

    async def core_requests():
        result = {}
        for kind, extractor_type in [
            ("character", CharacterSettingExtractor), ("world", WorldSettingExtractor),
        ]:
            client = Client({"candidates": []})
            extractor = extractor_type(
                llm_client=client, model="gpt-5.6-sol", max_attempts=1,
                max_output_tokens=32000, truncation_retry_max_output_tokens=64000,
            )
            kwargs = {"chunk_text": TEXT, "episode_no": 2, "episode_title": "합성 장면"}
            if kind == "character":
                kwargs.update(source_chunk_id=CHUNK_ID, schema_hints=HINTS)
            extracted = await extractor.extract_from_chunk(**kwargs)
            assert extracted.candidates == []
            request, = client.calls
            result[kind] = request_hash(request)
        return result

    expected = asyncio.run(core_requests())
    extract(Client({"observations": [observation()]}))
    assert asyncio.run(core_requests()) == expected
    assert set(CharacterSettingExtractionResult.model_fields) == {"candidates", "time_context"}
    assert set(character_setting_provider_json_schema()["properties"]) == {"candidates", "time_context"}
    assert "relationship_observations" not in inspect.signature(CharacterSettingExtractor).parameters
    assert "relationship_observations_output" not in inspect.signature(create_default_runtime_components).parameters


def test_existing_eval_cli_no_longer_accepts_same_call_relationship_flag(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "runtime_cli", "--gold", "synthetic-gold.json", "--output", "synthetic-output.json",
        "--mode", "FIXED", "--domains", "CHARACTER",
        "--relationship-observations-output", "old-sidecar.json",
    ])
    with pytest.raises(SystemExit) as exc:
        _parse_args()
    assert exc.value.code == 2
    assert "unrecognized arguments: --relationship-observations-output" in capsys.readouterr().err


def test_blank_source_span_is_rejected_without_dropping_or_rewriting_selection():
    values, rejected = ground_relationship_observations(
        [observation(evidence_span_ids=["S1", "S2"])], QUOTE + "\n\n", CHUNK_ID,
    )
    assert values == []
    assert rejected[0].reason == "EMPTY_SOURCE_SPAN"
