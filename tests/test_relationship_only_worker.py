"""Synthetic relationship-only jobs: no database, embeddings or provider requests."""

import asyncio
import json
from hashlib import sha256
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from uuid import uuid4

import httpx
import pytest

from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.relationship_observations import RelationshipExtractionResult
from app.analysis.relationship_pipeline import RelationshipAnalysisPipeline
from app.clients.spring_worker_client import SpringWorkerClient
from app.core.config import Settings
from app.domain.enums import AnalysisFailureCode
from app.llm.responses import LlmTextResponse
from app.schemas.worker import WorkerAnalysisJobPayload
from app.storage.s3 import SourceObjectTooLargeError
from app.worker.relationship_worker import RelationshipWorker


def make_payload(text="가와 나가 협력했다. 😀"):
    return WorkerAnalysisJobPayload.model_validate(
        {
            "analysisJobId": str(uuid4()),
            "workId": str(uuid4()),
            "workTitle": "합성 작품",
            "jobType": "RELATIONSHIP_EXTRACTION",
            "analysisMode": "CONFIRMED_ONLY",
            "sourceKind": "EPISODE",
            "relationshipAnalysisContract": {"formatVersion": 1},
            "leaseToken": str(uuid4()),
            "leaseExpiresAt": "2030-01-01T00:00:00Z",
            "claimAttemptCount": 1,
            "episode": {
                "episodeId": str(uuid4()),
                "episodeNo": 1,
                "title": "합성 회차",
                "contentS3Key": "synthetic/source.txt",
                "contentS3Version": "v1",
                "contentHash": sha256(text.encode()).hexdigest(),
                "charCount": len(text),
            },
        }
    )


def setup_worker(
    monkeypatch, *, status=None, observations=(), text="가와 나가 협력했다. 😀", **kwargs
):
    monkeypatch.setattr(
        "app.worker.relationship_worker.get_settings", lambda: Settings(_env_file=None)
    )
    payload = make_payload(text)
    context = {
        "formatVersion": 1,
        "sourceHash": payload.episode.content_hash,
        "sourceEpisodeNo": 1,
        "contextToken": "synthetic-token",
        "status": status or "PREPARED",
        "observations": list(observations),
        "nodes": [],
        "relationships": [],
        "heldObservations": [],
    }
    spring = SimpleNamespace(
        claim_relationship_job=AsyncMock(return_value=payload),
        heartbeat_relationship_job=AsyncMock(),
        complete_relationship_job=AsyncMock(),
        fail_relationship_job=AsyncMock(),
        get_relationship_analysis=AsyncMock(return_value=context if status else None),
        prepare_relationship_analysis=AsyncMock(return_value=context),
        complete_relationship_analysis=AsyncMock(),
        fail_relationship_analysis=AsyncMock(),
    )

    async def extract(**params):
        return RelationshipExtractionResult(
            source_chunk_id=params["source_chunk_id"],
            raw_observations=[],
            observations=[],
            rejections=[],
            response=LlmTextResponse(text="{}"),
        )

    extractor = SimpleNamespace(extract_from_chunk=AsyncMock(side_effect=extract))
    comparator = SimpleNamespace(compare=AsyncMock(return_value=[]))
    pipeline = RelationshipAnalysisPipeline(
        spring_client=spring, payload=payload, extractor=extractor, comparator=comparator
    )
    storage = SimpleNamespace(get_text=Mock(return_value=text))
    worker = RelationshipWorker(
        spring_client=spring, storage=storage, pipeline_factory=lambda _: pipeline, **kwargs
    )
    return worker, payload, spring, storage, extractor, comparator, pipeline


def test_fresh_job_uses_only_relationship_apis_and_pure_frozen_source_chunks(monkeypatch):
    worker, payload, spring, storage, extractor, comparator, _ = setup_worker(monkeypatch)
    assert worker._heartbeat_interval_seconds == 20.0
    # Fake Spring exposes no general completion, candidate, progress or DB API.
    result = asyncio.run(worker.process_claimed(payload))
    assert result.claimed
    storage.get_text.assert_called_once_with(
        "synthetic/source.txt", version_id="v1", max_bytes=2_000_000
    )
    assert (
        extractor.extract_from_chunk.call_args.kwargs["chunk_text"] == storage.get_text.return_value
    )
    comparator.compare.assert_not_called()
    spring.complete_relationship_analysis.assert_awaited_once()
    spring.complete_relationship_job.assert_awaited_once()
    spring.fail_relationship_job.assert_not_called()
    assert (
        json.loads(spring.complete_relationship_job.call_args.kwargs["summary_json"])[
            "relationshipAnalysisStatus"
        ]
        == "COMPLETED"
    )
    first = worker._read_chunks(payload)
    second = worker._read_chunks(payload)
    assert first == second
    assert first[0].start_offset == 0


@pytest.mark.parametrize("status", ["PREPARED", "COMPLETED", "APPLIED"])
def test_resume_does_not_repeat_source_or_extraction(monkeypatch, status):
    worker, payload, spring, storage, extractor, comparator, _ = setup_worker(
        monkeypatch, status=status, observations=[{"ref": "O1"}]
    )
    asyncio.run(worker.process_claimed(payload))
    storage.get_text.assert_not_called()
    extractor.extract_from_chunk.assert_not_called()
    spring.prepare_relationship_analysis.assert_not_called()
    assert comparator.compare.await_count == int(status == "PREPARED")
    spring.complete_relationship_job.assert_awaited_once()


def test_failed_relationship_stops_cursor_without_repeat_or_core_completion(monkeypatch):
    worker, payload, spring, storage, extractor, comparator, _ = setup_worker(
        monkeypatch, status="FAILED"
    )
    with pytest.raises(ComparisonValidationError):
        asyncio.run(worker.process_claimed(payload))
    storage.get_text.assert_not_called()
    extractor.extract_from_chunk.assert_not_called()
    comparator.compare.assert_not_called()
    spring.complete_relationship_job.assert_not_called()
    spring.fail_relationship_job.assert_awaited_once()


@pytest.mark.parametrize(
    "update",
    [
        {"job_type": "SETTING_EXTRACTION"},
        {"analysis_mode": "ORDERED_PROVISIONAL"},
        {"source_kind": "SETTING_BOOK"},
        {"relationship_analysis_contract": {"formatVersion": True}},
        {"relationship_analysis_contract": None},
    ],
)
def test_wrong_claim_contract_stops_before_source_or_model(monkeypatch, update):
    worker, payload, spring, storage, extractor, _, _ = setup_worker(monkeypatch)
    with pytest.raises(OrderedInputContextError):
        asyncio.run(worker.process_claimed(payload.model_copy(update=update)))
    spring.get_relationship_analysis.assert_not_called()
    storage.get_text.assert_not_called()
    extractor.extract_from_chunk.assert_not_called()
    spring.complete_relationship_job.assert_not_called()


def test_changed_source_hash_is_fatal_without_repair_or_extraction(monkeypatch):
    worker, payload, spring, storage, extractor, _, _ = setup_worker(monkeypatch)
    storage.get_text.return_value = "원문이 변경됨"
    with pytest.raises(OrderedInputContextError):
        asyncio.run(worker.process_claimed(payload))
    extractor.extract_from_chunk.assert_not_called()
    spring.prepare_relationship_analysis.assert_not_called()
    spring.complete_relationship_job.assert_not_called()
    assert (
        spring.fail_relationship_job.call_args.kwargs["error_message"]
        == "Relationship-only analysis failed."
    )


@pytest.mark.parametrize("limit_kind", ["bytes", "chunks"])
def test_source_limits_fail_without_truncation_or_any_paid_call(monkeypatch, limit_kind):
    text = ("한글 원문.\n" * 4000) if limit_kind == "chunks" else "한글 원문"
    worker, payload, spring, storage, extractor, comparator, _ = setup_worker(
        monkeypatch, text=text, max_chunks=1
    )
    if limit_kind == "bytes":
        storage.get_text.side_effect = SourceObjectTooLargeError("oversize")
    with pytest.raises(ComparisonValidationError):
        asyncio.run(worker.process_claimed(payload))
    extractor.extract_from_chunk.assert_not_called()
    comparator.compare.assert_not_called()
    assert spring.fail_relationship_analysis.call_args.args[2]["failureCode"] == "INPUT_TOO_LARGE"
    spring.complete_relationship_job.assert_not_called()


def test_mid_call_lease_failure_cancels_job_and_never_finalizes(monkeypatch):
    async def run():
        worker, payload, spring, _, extractor, _, _ = setup_worker(
            monkeypatch, heartbeat_interval_seconds=0.001
        )

        async def blocked(**_):
            await asyncio.Event().wait()

        extractor.extract_from_chunk.side_effect = blocked
        spring.heartbeat_relationship_job.side_effect = ValueError("synthetic lease lost")
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(asyncio.create_task(worker.process_claimed(payload)), 1)
        spring.heartbeat_relationship_job.assert_awaited_once_with(
            payload.analysis_job_id, payload.lease_token
        )
        spring.complete_relationship_job.assert_not_called()

    asyncio.run(run())


def test_metered_clients_use_only_relation_purposes_and_existing_terra_options(monkeypatch):
    monkeypatch.setattr(
        "app.worker.relationship_worker.get_settings", lambda: Settings(_env_file=None)
    )
    provider = object()
    worker = RelationshipWorker(spring_client=object(), llm_provider_client=provider)
    pipeline = worker._pipeline(make_payload())
    assert pipeline.extractor.call_options.reasoning_effort == "medium"
    for component, purpose in [
        (pipeline.extractor, "RELATIONSHIP_EXTRACTION"),
        (pipeline.comparator, "RELATIONSHIP_COMPARISON"),
    ]:
        assert component.model == "gpt-6.1-terra"
        assert component.llm_client.purpose == purpose
        assert component.llm_client.max_retries == 0


def test_relationship_job_wire_routes_lease_and_timeouts_are_separate():
    requests = []
    payload = make_payload()

    def respond(request):
        requests.append(request)
        if request.url.path.endswith("/claim"):
            return httpx.Response(
                200, json={"data": payload.model_dump(by_alias=True, mode="json")}
            )
        if request.url.path.endswith("/heartbeat"):
            return httpx.Response(
                200,
                json={
                    "data": {
                        "leaseToken": str(payload.lease_token),
                        "leaseExpiresAt": "2030-01-01T00:00:00Z",
                    }
                },
            )
        return httpx.Response(204)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond), timeout=30) as http:
            spring = SpringWorkerClient("http://test", "synthetic-internal-key", http)
            assert (
                await spring.claim_relationship_job()
            ).analysis_job_id == payload.analysis_job_id
            await spring.heartbeat_relationship_job(payload.analysis_job_id, payload.lease_token)
            await spring.complete_relationship_job(
                payload.analysis_job_id, payload.lease_token, "{}"
            )
            await spring.fail_relationship_job(
                payload.analysis_job_id,
                payload.lease_token,
                "Synthetic failure",
                AnalysisFailureCode.UNEXPECTED_ERROR,
            )

    asyncio.run(run())
    assert [r.url.path.rsplit("/", 1)[-1] for r in requests] == [
        "claim",
        "heartbeat",
        "complete",
        "fail",
    ]
    assert all("/relationship-jobs/" in r.url.path for r in requests)
    assert all(r.headers["X-Worker-Lease-Token"] == str(payload.lease_token) for r in requests[1:])
    assert json.loads(requests[0].content)["allowedJobTypes"] == ["RELATIONSHIP_EXTRACTION"]
    assert json.loads(requests[0].content)["supportedSourceKinds"] == ["EPISODE"]
    assert requests[1].extensions["timeout"]["read"] == 30.0
    assert requests[2].extensions["timeout"]["read"] == 120.0


def test_empty_dedicated_claim_has_no_side_effect():
    async def run():
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(lambda _: httpx.Response(204))
        ) as http:
            assert (
                await SpringWorkerClient("http://test", "synthetic", http).claim_relationship_job()
                is None
            )

    asyncio.run(run())


def test_cli_relationship_kind_never_constructs_core_or_database_worker(monkeypatch):
    from scripts import run_analysis_worker as runner

    def forbidden(*args, **kwargs):
        raise AssertionError("Relationship-only execution entered a core worker or database")

    worker = SimpleNamespace(claim_next=AsyncMock(return_value=None), aclose=AsyncMock())
    monkeypatch.setattr(runner, "RelationshipWorker", lambda: worker)
    monkeypatch.setattr(runner, "AnalysisJobWorker", forbidden)
    monkeypatch.setattr(runner, "CharacterFactComparisonWorker", forbidden)
    monkeypatch.setattr(runner, "WorldSettingComparisonWorker", forbidden)
    monkeypatch.setattr("app.db.session.get_session_maker", forbidden)
    monkeypatch.setattr(
        "sys.argv", ["run_analysis_worker.py", "--worker-kind", "relationship", "--once"]
    )
    settings = Settings(
        _env_file=None, ai_worker_metrics_enabled=False, ai_worker_tracing_enabled=False
    )
    asyncio.run(runner._async_main(runner._parse_args(settings), settings))
    worker.claim_next.assert_awaited_once()
    worker.aclose.assert_awaited_once()
