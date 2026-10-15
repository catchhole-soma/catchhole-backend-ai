"""Claim capability and fail-closed response contract at the actual Worker boundary."""

import asyncio
import json

import httpx
import pytest

from app.analysis.exceptions import OrderedInputContextError
from app.clients.spring_worker_client import SpringWorkerClient
from app.schemas.worker import WorkerAnalysisJobPayload
from app.worker.analysis_job_worker import AnalysisJobWorker, WorkerRunSummary
from tests.test_setting_book_source_contract import book_payload_data
from tests.test_analysis_job_worker import _payload


def test_analysis_worker_advertises_frozen_evidence_through_http_claim():
    requests = []

    def respond(request):
        requests.append(json.loads(request.content))
        return httpx.Response(204)

    async def scenario():
        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as http:
            spring = SpringWorkerClient("http://spring.test", "test-only", http)
            worker = AnalysisJobWorker(spring_client=spring)
            try:
                result = await worker.run_once()
                assert result.claimed is False
            finally:
                await worker.aclose()

    asyncio.run(scenario())
    assert requests[0]["supportsFrozenComparisonEvidence"] is True
    assert requests[0]["supportsSemanticCurrentContext"] is True
    assert requests[0]["supportedAnalysisModes"] == ["CONFIRMED_ONLY", "ORDERED_PROVISIONAL"]


@pytest.mark.parametrize("contract", [None, {}, {"formatVersion": 2}, {"formatVersion": True},
                                    {"formatVersion": "1"}, [], "invalid"])
def test_invalid_ordered_evidence_contract_reports_lease_failure_before_analysis(contract):
    data = book_payload_data()
    data["comparisonEvidenceContract"] = contract
    payload = WorkerAnalysisJobPayload.model_validate(data)
    calls = []

    class Spring:
        async def report_progress(self, **kwargs):
            pytest.fail("Invalid contract reached progress mutation")

        async def fail(self, **kwargs):
            calls.append(kwargs)

    class Worker(AnalysisJobWorker):
        async def _run_analysis_steps(self, payload):
            pytest.fail("Invalid contract reached chunks, provider, or persistence")

    async def scenario():
        worker = Worker(spring_client=Spring())
        try:
            with pytest.raises(OrderedInputContextError):
                await worker.process_claimed(payload)
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["analysis_job_id"] == payload.analysis_job_id
    assert calls[0]["lease_token"] == payload.lease_token


@pytest.mark.parametrize("mode,contract", [("ORDERED_PROVISIONAL", {"formatVersion": 1}),
                                          ("CONFIRMED_ONLY", None)])
def test_supported_contract_and_legacy_confirmed_jobs_reach_normal_completion(mode, contract):
    data = book_payload_data() if mode == "ORDERED_PROVISIONAL" else _payload().model_dump(by_alias=True, mode="json")
    data.update(analysisMode=mode, comparisonEvidenceContract=contract)
    payload = WorkerAnalysisJobPayload.model_validate(data)
    calls = []

    class Spring:
        async def report_progress(self, **kwargs):
            calls.append("progress")

        async def complete(self, **kwargs):
            calls.append("complete")

        async def fail(self, **kwargs):
            pytest.fail("Valid contract reported failure")

    class Worker(AnalysisJobWorker):
        async def _run_analysis_steps(self, payload):
            calls.append("analysis")
            return WorkerRunSummary()

    async def scenario():
        worker = Worker(spring_client=Spring())
        try:
            assert (await worker.process_claimed(payload)).claimed is True
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert calls == ["progress", "analysis", "progress", "complete"]
