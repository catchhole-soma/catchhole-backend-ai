import asyncio
import json
from pathlib import Path
from uuid import UUID

import httpx
import pytest

from app.analysis.character_status_lifecycle import reconcile_status_lifecycle
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.openai_client import OpenAIResponsesClient
from evals.character_status_stability.lifecycle_probe import (
    RecordedClient,
    load_recorded_batches,
    parser,
    replay,
)


def test_recorded_outer_refs_restore_projection_without_original_text_or_gold(tmp_path):
    _record_batch(tmp_path)
    (batch,) = load_recorded_batches(tmp_path)

    assert [c.candidate_ref for c in batch.context.candidates] == ["C1", "C2"]
    assert batch.decisions[1].removed_snapshot_refs == ["P1"]
    assert batch.context.candidates[1].evidence_spans[0].quote == "상처가 아물고 혼자 달렸다."
    remaining = replay(batch, batch.decisions)["remainingStatuses"]
    assert {entry["reference"] for entry in remaining} == {"P2", "Q1"}
    assert not (tmp_path / "input.json").exists()
    assert not (tmp_path / "expectations.json").exists()


@pytest.mark.parametrize("problem", ["token", "coverage", "failure", "unsuccessful"])
def test_incomplete_or_mismatched_recordings_fail_before_provider_use(tmp_path, problem):
    _record_batch(tmp_path)
    path = tmp_path / "worker/0002-spring-request.json"
    data = json.loads(path.read_text())
    if problem == "token":
        data["body"]["contextToken"] = "b" * 64
    elif problem == "coverage":
        data["body"]["decisions"].pop()
    elif problem == "failure":
        data["body"]["failures"] = [{"candidateRef": "C1"}]
    else:
        response = tmp_path / "worker/0003-spring-response.json"
        recorded = json.loads(response.read_text())
        recorded["status"] = 409
        response.write_text(json.dumps(recorded))
    path.write_text(json.dumps(data))

    with pytest.raises(ValueError):
        load_recorded_batches(tmp_path)


def test_real_lifecycle_retry_records_raw_usage_and_store_false_without_credentials(tmp_path):
    _record_batch(tmp_path)
    (batch,) = load_recorded_batches(tmp_path)
    responses = [
        {"checks": []},
        {
            "checks": [
                {
                    "snapshot_ref": "P2",
                    "verdict": "KEEP",
                    "carrier_candidate_ref": None,
                    "reason": "저주가 풀렸다는 관찰은 없다.",
                },
                {
                    "snapshot_ref": "Q1",
                    "verdict": "END",
                    "carrier_candidate_ref": "C2",
                    "reason": "상처가 아물고 독립적으로 움직여 급한 위험이 끝났다.",
                },
            ]
        },
    ]
    recorder = RecordedClient(tmp_path / "private")

    def handler(request):
        assert json.loads(request.content)["store"] is False
        return httpx.Response(
            200,
            json={
                "status": "completed",
                "output_text": json.dumps(responses.pop(0)),
                "usage": {
                    "input_tokens": 20,
                    "input_tokens_details": {"cached_tokens": 8},
                    "output_tokens": 5,
                },
            },
        )

    async def exercise():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            event_hooks={"response": [recorder.record_response]},
        )
        provider = OpenAIResponsesClient(
            api_key="private-key-must-not-be-recorded",
            model="gpt-5.6-luna",
            responses_api_url="https://api.openai.com/v1/responses",
            reasoning_effort="none",
            http_client=client,
        )
        recorder.delegate = provider
        try:
            return await reconcile_status_lifecycle(
                llm_client=recorder,
                model="gpt-5.6-luna",
                max_output_tokens=1000,
                max_attempts=2,
                max_input_tokens=64000,
                matched_character_name="리안",
                candidates=batch.context.candidates,
                initial_entries=batch.initial_entries(),
                decisions=batch.decisions,
            )
        finally:
            await provider.aclose()

    result = asyncio.run(exercise())

    assert result[1].removed_snapshot_refs == ["P1", "Q1"]
    assert replay(batch, result)["applications"][1]["dependencyCandidateRefs"] == ("C1",)
    assert len(recorder.calls) == 2
    assert all(not call["usageUnknown"] for call in recorder.calls)
    assert sum(call["inputTokens"] for call in recorder.calls) == 40
    assert sum(call["cachedInputTokens"] for call in recorder.calls) == 16
    assert sum(call["outputTokens"] for call in recorder.calls) == 10
    assert recorder.calls[0]["rawResponse"]["status"] == "completed"
    assert "validation_feedback" in json.dumps(recorder.calls[1]["request"])
    for path in (tmp_path / "private").glob("*.json"):
        assert path.stat().st_mode & 0o777 == 0o600
        assert "private-key-must-not-be-recorded" not in path.read_text()
        assert "Authorization" not in path.read_text()


def test_failed_provider_response_keeps_usage_and_raw_response(tmp_path):
    recorder = RecordedClient(tmp_path)

    async def exercise():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda request: httpx.Response(
                    200,
                    json={
                        "status": "incomplete",
                        "incomplete_details": {"reason": "max_output_tokens"},
                        "usage": {
                            "input_tokens": 30,
                            "input_tokens_details": {"cached_tokens": 9},
                            "output_tokens": 100,
                        },
                        "output_text": "{",
                    },
                )
            ),
            event_hooks={"response": [recorder.record_response]},
        )
        provider = OpenAIResponsesClient(
            api_key="fake",
            model="gpt-5.6-luna",
            responses_api_url="https://api.openai.com/v1/responses",
            http_client=client,
        )
        recorder.delegate = provider
        try:
            await recorder.create_text_response(
                system_prompt="system",
                user_prompt="recorded",
                model="gpt-5.6-luna",
                max_output_tokens=100,
            )
        finally:
            await provider.aclose()

    with pytest.raises(LlmOutputTruncatedError):
        asyncio.run(exercise())

    assert recorder.calls[0]["errorType"] == "LlmOutputTruncatedError"
    assert recorder.calls[0]["inputTokens"] == 30
    assert recorder.calls[0]["cachedInputTokens"] == 9
    assert recorder.calls[0]["outputTokens"] == 100
    assert recorder.calls[0]["usageUnknown"] is False
    assert recorder.calls[0]["rawResponse"]["status"] == "incomplete"


def test_cli_needs_recording_and_key_but_has_no_source_or_expectation_argument():
    args = parser().parse_args(
        [
            "--episode-dir",
            "recording",
            "--java-repo",
            "java",
            "--key-env-file",
            "private.env",
            "--runs",
            "3",
        ]
    )
    assert args.runs == 3
    assert args.episode_dir == Path("recording")
    assert not hasattr(args, "source_dir")
    assert not hasattr(args, "expectations")


def test_network_failure_records_request_and_unknown_usage_before_response(tmp_path):
    recorder = RecordedClient(tmp_path)

    def handler(request):
        raise httpx.ConnectError("offline", request=request)

    async def exercise():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler),
            event_hooks={
                "request": [recorder.record_request],
                "response": [recorder.record_response],
            },
        )
        provider = OpenAIResponsesClient(
            api_key="private-key",
            model="gpt-5.6-luna",
            responses_api_url="https://api.openai.com/v1/responses",
            http_client=client,
        )
        recorder.delegate = provider
        try:
            await recorder.create_text_response(
                system_prompt="system", user_prompt="recorded", model="gpt-5.6-luna"
            )
        finally:
            await provider.aclose()

    with pytest.raises(httpx.ConnectError):
        asyncio.run(exercise())

    call = json.loads((tmp_path / "call-0001.json").read_text())
    assert call["request"]["store"] is False
    assert call["usageUnknown"] is True
    assert "inputTokens" not in call
    assert call["errorType"] == "ConnectError"
    assert "private-key" not in json.dumps(call)


def _record_batch(directory):
    worker = directory / "worker"
    worker.mkdir()
    route = f"/api/internal/v1/analysis-jobs/{UUID(int=1)}/character-fact-comparison-batches/{UUID(int=2)}"
    context = {
        "comparisonBatchId": str(UUID(int=2)),
        "characterRef": "K1",
        "matchedCharacterName": "리안",
        "canonicalFactType": "STATUS",
        "baseSnapshotVersion": 2,
        "contextToken": "a" * 64,
        "candidates": [
            {
                "candidateRef": ref,
                "projectedSnapshotRef": f"Q{ref[1:]}",
                "sourceEpisodeNo": 5,
                "rawFactKey": key,
                "initialCanonicalFactKey": key,
                "canonicalKeyResolution": "PATTERN",
                "attributeValue": quote,
                "valueType": "JSON",
                "valueJson": {"active": active},
                "evidenceSpans": [{"quote": quote}],
                "confidence": 0.9,
            }
            for ref, key, quote, active in [
                ("C1", "status.위기", "출혈로 위험했다.", True),
                ("C2", "status.발", "상처가 아물고 혼자 달렸다.", False),
            ]
        ],
        "snapshotEntries": [
            {
                "snapshotRef": ref,
                "origin": "PERSISTED",
                "factType": "STATUS",
                "factKey": key,
                "factValue": value,
                "valueJson": {"active": True},
            }
            for ref, key, value in [("P1", "status.발", "발 부상"), ("P2", "status.저주", "저주")]
        ],
    }
    decisions = [
        {
            "candidateRef": "C1",
            "operation": "ADD",
            "resolvedCanonicalFactKey": "status.위기",
            "proposedFactValue": "출혈로 위험했다.",
            "proposedValueJson": {"active": True},
            "temporalScope": "PRESENT",
            "comparisonReason": "생명에 위험이 생겼다.",
        },
        {
            "candidateRef": "C2",
            "operation": "REMOVE",
            "resolvedCanonicalFactKey": "status.발",
            "removedSnapshotRefs": ["P1"],
            "temporalScope": "PRESENT",
            "comparisonReason": "발이 회복됐다.",
            "rawComparisonJson": {"operation": "REMOVE"},
        },
    ]
    for name, payload in [
        (
            "0001-spring-response.json",
            {"path": route + "/context", "status": 200, "body": {"data": context}},
        ),
        (
            "0002-spring-request.json",
            {
                "path": route + "/complete",
                "body": {"contextToken": "a" * 64, "decisions": decisions},
            },
        ),
        (
            "0003-spring-response.json",
            {"path": route + "/complete", "status": 200, "body": {"success": True}},
        ),
    ]:
        (worker / name).write_text(json.dumps(payload, ensure_ascii=False))
