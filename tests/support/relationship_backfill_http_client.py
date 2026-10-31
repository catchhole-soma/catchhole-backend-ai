"""GH332 synthetic Java HTTP bridge; real Worker, fake storage/provider, no DB access."""

import asyncio
import json
import os
import sys
from unittest.mock import patch
from urllib.parse import urlsplit

import httpx

from app.clients.spring_worker_client import SpringWorkerClient
from app.llm.responses import LlmTextResponse
from app.storage.s3 import SourceObjectTooLargeError
from app.worker.relationship_worker import RelationshipWorker


class SyntheticStorage:
    def __init__(self, sources):
        self.sources = sources
        self.reads = 0

    def get_text(self, key, version_id=None, *, max_bytes=None):
        self.reads += 1
        text = self.sources[key]
        if max_bytes is not None and len(text.encode("utf-8")) > max_bytes:
            raise SourceObjectTooLargeError("Synthetic source is too large")
        return text


class SyntheticRelationshipProvider:
    def __init__(self):
        self.calls = []

    async def create_text_response(self, *, user_prompt, model, response_schema, call_options, **_):
        assert model == "gpt-6.1-terra"
        assert call_options.reasoning_effort == "medium"
        data = json.loads(user_prompt)
        kind = response_schema.name
        if kind == "relationship_observations":
            spans = data["source_spans"]
            assert len(spans) == 1 and "아라" in spans[0]["text"] and "보라" in spans[0]["text"]
            result = {
                "observations": [
                    {
                        "source": {"name": "아라", "mention": "아라"},
                        "target": {"name": "보라", "mention": "보라"},
                        "summary": spans[0]["text"],
                        "evidence_span_ids": [spans[0]["ref"]],
                    }
                ]
            }
        elif kind == "relationship_comparison":
            assert len(data["observations"]) == 1
            observation = data["observations"][0]
            assert len(observation["sourceNodeRefs"]) == len(observation["targetNodeRefs"]) == 1
            source, target = observation["sourceNodeRefs"][0], observation["targetNodeRefs"][0]
            matches = [
                row
                for row in data["relationships"]
                if {row["sourceNodeRef"], row["targetNodeRef"]} == {source, target}
                and row["axis"] == "SOCIAL"
                and row["kind"] == "COMPANIONSHIP"
                and row["direction"] == "MUTUAL"
            ]
            assert len(matches) <= 1
            relation = matches[0] if matches else None
            result = {
                "decisions": [
                    {
                        "observationRefs": [observation["ref"]],
                        "operation": "UPDATE" if relation else "ADD",
                        "relationshipRef": relation["ref"] if relation else None,
                        "sourceNodeRef": source,
                        "targetNodeRef": target,
                        "axis": "SOCIAL",
                        "kind": "COMPANIONSHIP",
                        "direction": "MUTUAL",
                        "label": "동료",
                        "summary": observation["summary"],
                        "temporalScope": "PRESENT",
                        "reason": None,
                    }
                ]
            }
        else:
            raise AssertionError("Non-relationship model call is forbidden in this bridge")
        self.calls.append(kind)
        return LlmTextResponse(
            text=json.dumps(result, ensure_ascii=False),
            input_token_count=100,
            cached_input_token_count=20,
            output_token_count=10,
        )


def forbidden(*args, **kwargs):
    raise AssertionError("Database or real provider construction is forbidden in this bridge")


async def main():
    base_url = os.environ["SPRING_INTERNAL_API_BASE_URL"]
    url = urlsplit(base_url)
    if (
        os.getenv("GH332_TEST_ONLY") != "true"
        or url.scheme != "http"
        or url.hostname not in {"127.0.0.1", "localhost"}
        or os.environ["SPRING_INTERNAL_API_KEY"] != "synthetic-gh332-key"
    ):
        raise RuntimeError("Local synthetic Spring server required")
    request = json.load(sys.stdin)
    storage = SyntheticStorage(request["sources"])
    provider = SyntheticRelationshipProvider()
    async with httpx.AsyncClient(timeout=30, trust_env=False) as http:
        client = SpringWorkerClient(base_url, os.environ["SPRING_INTERNAL_API_KEY"], http)
        with (
            patch("app.db.session.get_session_maker", forbidden),
            patch("app.worker.analysis_job_worker.get_session_maker", forbidden),
            patch("app.llm.openai_client.OpenAIResponsesClient.from_settings", forbidden),
        ):
            worker = RelationshipWorker(
                spring_client=client, storage=storage, llm_provider_client=provider
            )
            try:
                payload = await worker.claim_next()
                expected = request.get("expectedClaim", True)
                assert (payload is not None) == expected
                episode_no = None
                if payload is not None:
                    assert str(payload.work_id) == request["workId"]
                    episode_no = payload.episode.episode_no
                    assert episode_no == request["expectedEpisodeNo"]
                    await worker.process_claimed(payload)
            finally:
                await worker.aclose()
    print(
        json.dumps(
            {
                "claimed": payload is not None,
                "episodeNo": episode_no,
                "providerCalls": len(provider.calls),
                "purposes": provider.calls,
                "inputTokens": 100 * len(provider.calls),
                "cachedInputTokens": 20 * len(provider.calls),
                "outputTokens": 10 * len(provider.calls),
                "sourceReads": storage.reads,
            }
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
