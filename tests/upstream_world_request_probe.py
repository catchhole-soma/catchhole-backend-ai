"""Independent world extraction/singleton HTTP capture; no provider or credentials."""
import asyncio
import json
from pathlib import Path
import sys
from uuid import UUID

import httpx

from app.analysis.world_setting_comparator import WorldSettingComparator
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.core.config import Settings, get_settings
from app.llm.openai_client import OpenAIResponsesClient
from app.schemas.worker import WorkerWorldSettingCandidatePayload
from legacy_request_probe import digest


async def collect():
    Settings.model_config = {**Settings.model_config, "env_file": None}
    get_settings.cache_clear()
    requests = {}
    purpose = "world_extraction"
    responses = [{"candidates": []}, {
        "consolidation_status": "SINGLE", "operation": "ADD", "review_reason": None,
        "target_ref": None, "matched_scope_name": None, "matched_property_name": None,
        "proposed_scope_name": None, "proposed_setting_name": "위치", "proposed_value": "바닷가",
        "comparison_reason": "등대의 위치를 새로 기록한다.",
    }]

    def transport(request):
        body = json.loads(request.content)
        assert purpose not in requests, "Unexpected retry in successful synthetic request"
        requests[purpose] = {
            "sha256": digest(body), "keys": sorted(body), "model": body["model"],
            "reasoning": body.get("reasoning"), "store": body["store"],
            "max_output_tokens": body["max_output_tokens"], "prompt_cache_key": body.get("prompt_cache_key"),
            "system_sha256": digest(body["input"][0]), "user_sha256": digest(body["input"][1]),
            "schema_sha256": digest(body.get("text")),
        }
        return httpx.Response(200, json={"status": "completed", "output": [{"type": "message",
            "content": [{"type": "output_text", "text": json.dumps(responses.pop(0), ensure_ascii=False)}]}],
            "usage": {"input_tokens": 1, "output_tokens": 1}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as http:
        client = OpenAIResponsesClient(api_key="synthetic-only", model="gpt-5.6-terra",
            responses_api_url="https://provider.test.invalid/responses", reasoning_effort="none", http_client=http)
        await WorldSettingExtractor(llm_client=client, model="gpt-5.6-terra", max_attempts=1).extract_from_chunk(
            UUID(int=1), "등대는 바닷가에 있다.")
        purpose = "world_singleton"
        candidate = WorkerWorldSettingCandidatePayload(candidate_id=UUID(int=2), work_id=UUID(int=3),
            source_episode_id=UUID(int=4), category="LOCATION", subject_name="등대", setting_name="위치",
            extracted_value="바닷가", evidence_spans=[{"quote": "등대는 바닷가에 있다."}], extraction_confidence=0.9)
        await WorldSettingComparator(llm_client=client, model="gpt-5.6-luna", max_attempts=1).compare(candidate, [])
    assert not responses
    return requests


if __name__ == "__main__":
    import app
    assert Path(app.__file__).resolve().is_relative_to(Path(sys.argv[1]).resolve())
    print(json.dumps(asyncio.run(collect()), ensure_ascii=False, sort_keys=True))
