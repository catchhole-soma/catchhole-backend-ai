"""BOOK world extraction must satisfy the real provider's strict JSON contract."""

import asyncio
import json

import httpx
import pytest

from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.llm.openai_client import OpenAIResponsesClient
from app.schemas.worker import WorkerAnalysisJobPayload
from tests.test_ordered_subject_rules import no_network as no_network
from tests.test_setting_book_source_contract import book_payload_data

QUOTE = "도시의 문은 해가 지면 닫힌다."
OUTPUT = {"candidates": [{
    "category": "WORLD_RULE_HISTORY", "subject_name": "도시", "scope_name": None,
    "setting_name": "출입 시간", "extracted_value": "해가 지면 문이 닫힌다",
    "evidence_spans": [{"quote": QUOTE, "start_offset": None, "end_offset": None}],
    "confidence": 0.95, "temporal_scope": "PRESENT", "effective_from_episode_no": None,
    "epistemic_status": "ASSERTED",
}]}


def _strict_object_violations(node, path="$", violations=None):
    """Independent provider boundary: closed objects and every property required."""
    violations = [] if violations is None else violations
    if isinstance(node, dict):
        if node.get("type") == "object":
            if node.get("additionalProperties") is not False:
                violations.append(path + ":additionalProperties")
            if set(node.get("required", [])) != set(node.get("properties", {})):
                violations.append(path + ":required")
        for key, value in node.items():
            _strict_object_violations(value, path + "/" + key, violations)
    elif isinstance(node, list):
        for index, value in enumerate(node):
            _strict_object_violations(value, path + "/" + str(index), violations)
    return violations


def _extract(*, book, ordered=False):
    async def scenario():
        async def provider(request):
            body = json.loads(request.content)
            response_format = body.get("text", {}).get("format")
            if response_format is not None and _strict_object_violations(response_format["schema"]):
                return httpx.Response(400, json={"error": {
                    "code": "invalid_json_schema", "type": "invalid_request_error",
                    "param": "text.format.schema", "message": "Strict object schema is incomplete.",
                }})
            return httpx.Response(200, json={
                "status": "completed", "output_text": json.dumps(OUTPUT),
                "usage": {"input_tokens": 10, "output_tokens": 20},
            })

        client = OpenAIResponsesClient(
            api_key="offline-test", model="offline-test-model",
            responses_api_url="https://provider.invalid/v1/responses",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
        )
        try:
            payload = WorkerAnalysisJobPayload.model_validate(book_payload_data())
            return await WorldSettingExtractor(llm_client=client, model="offline-test-model", max_attempts=1).extract_from_chunk(
                QUOTE,
                **({"setting_book": payload.setting_book} if book else {}),
                **({"analysis_context": payload.analysis_context} if ordered else {}),
            )
        finally:
            await client.aclose()

    return asyncio.run(scenario())


@pytest.mark.parametrize("ordered", [False, True])
def test_book_world_extraction_passes_strict_provider_and_preserves_nullable_fields(ordered):
    result = _extract(book=True, ordered=ordered)
    assert len(result.candidates) == 1
    candidate = result.candidates[0]
    assert candidate.scope_name is None and candidate.effective_from_episode_no is None
    assert candidate.temporal_scope == "PRESENT"
    assert candidate.evidence_spans[0].quote == QUOTE
    assert candidate.evidence_spans[0].start_offset is None
    assert candidate.evidence_spans[0].end_offset is None


def test_legacy_episode_world_extraction_keeps_its_existing_response_contract():
    result = _extract(book=False)
    assert len(result.candidates) == 1
    assert result.candidates[0].extracted_value == "해가 지면 문이 닫힌다"
