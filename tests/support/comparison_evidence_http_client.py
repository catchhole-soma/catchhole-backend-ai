"""GH304 local synthetic Spring bridge. No database or model client is used."""

import asyncio
import json
import os
import sys
from urllib.parse import urlsplit
from uuid import UUID

import httpx

from app.clients.exceptions import SpringWorkerHttpError
from app.clients.spring_worker_client import SpringWorkerClient
from app.exceptions.failure_classification import comparison_failure_code, failure_diagnostics


async def main():
    base_url = os.environ["SPRING_INTERNAL_API_BASE_URL"]
    if os.getenv("GH304_TEST_ONLY") != "true" or urlsplit(base_url).hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("Local synthetic Spring server required")
    payload = json.load(sys.stdin)
    job, batch, lease = (UUID(payload[key]) for key in ("job", "batch", "lease"))
    async with httpx.AsyncClient() as http:
        client = SpringWorkerClient(base_url, os.environ["SPRING_INTERNAL_API_KEY"], http)
        try:
            await client.get_character_fact_comparison_batch_context(job, batch, lease)
        except SpringWorkerHttpError as error:
            diagnostic = failure_diagnostics(error, stage="FINALIZE")
            assert error.status_code == 409
            assert diagnostic.reason_code == "CURRENT_EVIDENCE_SOURCE_HASH_MISMATCH"
            assert diagnostic.stage == "CHARACTER_CONTEXT" and diagnostic.http_status == 409
            assert str(diagnostic.source_analysis_job_id) == payload["sourceJob"]
            # The source-read boundary identifies the manifest Job, not an individual candidate.
            assert diagnostic.source_candidate_id is None
            assert diagnostic.request_id is not None
            code = comparison_failure_code(error)
            assert code.value == "UNEXPECTED_ERROR"
            await client.fail_character_fact_comparison_batch(job, batch, lease, str(error), code,
                                                              failure_diagnostics=diagnostic)
            await client.fail(job, lease, str(error), code, failure_diagnostics=diagnostic)
            legacy = SpringWorkerClient(base_url + "/api/internal/legacy", os.environ["SPRING_INTERNAL_API_KEY"], http)
            await legacy.fail(job, lease, str(error), code, failure_diagnostics=diagnostic)
            for method in (legacy.fail_character_fact_comparison, legacy.fail_character_fact_comparison_batch,
                           legacy.fail_world_setting_comparison, legacy.fail_world_setting_comparison_batch):
                await method(job, batch, lease, str(error), code, failure_diagnostics=diagnostic)
            print(json.dumps({"reason": diagnostic.reason_code, "reportedBatchAndJob": True}))
        else:
            raise AssertionError("Expected real source-hash rejection")


if __name__ == "__main__":
    asyncio.run(main())
