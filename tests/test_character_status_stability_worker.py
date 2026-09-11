import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonRunResult
from app.core.config import Settings
from app.domain.enums import AnalysisFailureCode
from app.worker.analysis_job_worker import AnalysisJobWorker, WorkerRunSummary
from app.worker.character_fact_comparison_worker import CharacterFactComparisonWorker
from evals.character_status_stability import worker as evaluation


def test_local_source_preserves_bytes_and_rejects_traversal_and_symlinks(tmp_path) -> None:
    root = tmp_path / "objects"
    root.mkdir()
    (root / "source.txt").write_bytes("회복\r\n결과\n".encode())
    outside = tmp_path / "outside.txt"
    outside.write_text("outside", encoding="utf-8")
    (root / "link.txt").symlink_to(outside)
    storage = evaluation.LocalSourceStorage(root)

    assert storage.get_text("source.txt") == "회복\r\n결과\n"
    for key in ("../outside.txt", "link.txt", str(outside)):
        with pytest.raises(ValueError, match="escapes"):
            storage.get_text(key)


def test_audit_redacts_auth_without_mutating_context_and_preserves_existing_runs(tmp_path) -> None:
    body = {
        "leaseToken": "lease-secret",
        "data": {"authorization": "auth-secret", "contextToken": "snapshot-version"},
        "candidates": [{"attributeName": "status.부상", "valueJson": {"active": False}}],
    }
    audit = evaluation._Audit(tmp_path / "audit")
    audit.write("context", body)
    path = next(audit.directory.iterdir())
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert "leaseToken" not in saved
    assert saved["data"] == {"contextToken": "snapshot-version"}
    assert body["leaseToken"] == "lease-secret"
    assert saved["candidates"] == body["candidates"]
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(ValueError, match="preserve prior runs"):
        evaluation._Audit(audit.directory)


def test_extractor_observer_preserves_the_returned_object_and_never_records_prompt(
    tmp_path,
) -> None:
    class Candidate:
        def model_dump(self, **kwargs):
            return {"attribute_name": "status.부상", "value_json": {"active": False}}

    result = SimpleNamespace(candidates=[Candidate()])

    class Delegate:
        async def extract_from_chunk(self, **kwargs):
            assert kwargs["chunk_text"] == "private complete manuscript"
            return result

    audit = evaluation._Audit(tmp_path / "audit")
    observed = asyncio.run(
        evaluation._AuditedExtractor(Delegate(), audit).extract_from_chunk(
            source_chunk_id=UUID(int=1),
            episode_no=5,
            chunk_text="private complete manuscript",
        )
    )
    assert observed is result
    content = next(audit.directory.iterdir()).read_text(encoding="utf-8")
    assert "private complete manuscript" not in content
    assert '"active": false' in content


def _settings() -> Settings:
    return Settings(
        _env_file=None,
        database_url="postgresql+psycopg://local:private-db-password@127.0.0.1:25432/eval",
        spring_internal_api_base_url="http://127.0.0.1:18086",
        spring_internal_api_key="internal-secret",
        llm_api_key="provider-secret",
        llm_extraction_model="gpt-5.6-terra",
        llm_subject_resolution_model="gpt-5.6-luna",
        llm_comparison_model="gpt-5.6-luna",
        llm_reasoning_effort="none",
    )


@pytest.mark.parametrize("fail_stage", [False, True])
def test_run_one_job_uses_real_claim_lifecycle_and_records_failures_safely(
    tmp_path,
    monkeypatch,
    capsys,
    fail_stage,
) -> None:
    settings = _settings()
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    monkeypatch.setattr("app.worker.analysis_job_worker.get_settings", lambda: settings)
    monkeypatch.setattr(evaluation, "get_session_maker", lambda: lambda: None)
    payload = {
        "analysisJobId": str(UUID(int=1)),
        "jobType": "SETTING_EXTRACTION",
        "workId": str(UUID(int=2)),
        "workTitle": "fixture",
        "batchId": str(UUID(int=3)),
        "leaseToken": str(UUID(int=99)),
        "leaseExpiresAt": "2030-01-01T00:00:00Z",
        "claimAttemptCount": 1,
        "episode": {
            "episodeId": str(UUID(int=4)),
            "episodeNo": 5,
            "contentS3Key": "uploaded/source.txt",
            "charCount": 12,
        },
    }
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        assert request.headers["X-Internal-Api-Key"] == "internal-secret"
        if request.url.path.endswith("/claim"):
            return httpx.Response(200, json={"data": payload})
        assert request.headers["X-Worker-Lease-Token"] == str(UUID(int=99))
        return httpx.Response(200, json={"data": {}})

    real_client = evaluation._AuditedSpringClient

    def client_factory(**kwargs):
        return real_client(
            **kwargs,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(evaluation, "_AuditedSpringClient", client_factory)

    class LifecycleProbe(evaluation._AuditedWorker):
        async def _run_analysis_steps(self, claim):
            assert claim.lease_token == UUID(int=99)
            assert self.embedding_generation_enabled is False
            empty = await self._world_setting_extractor.extract_from_chunk(chunk_text="private")
            assert empty.candidates == []
            if fail_stage:
                print("private manuscript must not reach stdout")
                raise ValueError("private failure text must not reach result")
            return WorkerRunSummary(
                summary_json=json.dumps(
                    {
                        "characterFactComparisonCompletedCount": 2,
                        "characterFactComparisonFailedCount": 0,
                    }
                )
            )

    # Keep production process_claimed: only stand in for its expensive stage body.
    assert LifecycleProbe.process_claimed is AnalysisJobWorker.process_claimed
    monkeypatch.setattr(evaluation, "_AuditedWorker", LifecycleProbe)
    result = asyncio.run(
        evaluation.run_one_job(
            settings=settings,
            storage_root=tmp_path,
            audit_dir=tmp_path / "audit",
        )
    )

    assert result["claimed"] is True
    assert result["success"] is not fail_stage
    assert result["analysisJobId"] == str(UUID(int=1))
    assert result["episodeId"] == str(UUID(int=4))
    assert "leaseToken" not in result["claim"]
    assert requests[0].url.path.endswith("/claim")
    assert requests[1].url.path.endswith("/progress")
    assert requests[-1].url.path.endswith("/fail" if fail_stage else "/complete")
    serialized = json.dumps(result)
    assert "provider-secret" not in serialized
    assert "private failure text" not in serialized
    audit_text = "".join(path.read_text() for path in (tmp_path / "audit").iterdir())
    assert "internal-secret" not in audit_text
    assert str(UUID(int=99)) not in audit_text
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(
    "field,value",
    [
        ("spring_internal_api_base_url", "https://production.example.com"),
        ("database_url", "postgresql+psycopg://user:pass@production.example.com/db"),
    ],
)
def test_evaluation_rejects_nonlocal_resources_before_any_claim(monkeypatch, field, value) -> None:
    settings = _settings().model_copy(update={field: value})
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    with pytest.raises(ValueError, match="localhost"):
        evaluation._validate_runtime(settings)


def _comparison_payload() -> dict:
    return {
        "analysisJobId": str(UUID(int=1)),
        "jobType": "CHARACTER_FACT_COMPARISON",
        "workId": str(UUID(int=2)),
        "workTitle": "fixture",
        "settingCandidateId": str(UUID(int=5)),
        "leaseToken": str(UUID(int=99)),
        "leaseExpiresAt": "2030-01-01T00:00:00Z",
        "claimAttemptCount": 1,
    }


def _install_comparison_http(monkeypatch, payload):
    settings = _settings()
    monkeypatch.setattr(evaluation, "get_settings", lambda: settings)
    monkeypatch.setattr("app.worker.character_fact_comparison_worker.get_settings", lambda: settings)
    requests = []
    real_client = evaluation._AuditedSpringClient

    def handler(request):
        requests.append(request)
        assert request.headers["X-Internal-Api-Key"] == "internal-secret"
        if request.url.path.endswith("/claim"):
            if payload is None:
                return httpx.Response(204)
            return httpx.Response(200, json={"data": payload})
        assert request.headers["X-Worker-Lease-Token"] == str(UUID(int=99))
        return httpx.Response(200, json={"data": {}})

    def client_factory(**kwargs):
        return real_client(
            **kwargs, http_client=httpx.AsyncClient(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr(evaluation, "_AuditedSpringClient", client_factory)
    return settings, requests


@pytest.mark.parametrize("candidate_failed", [False, True])
def test_comparison_wrapper_uses_production_lifecycle_and_preserves_typed_failure(
    monkeypatch, tmp_path, capsys, candidate_failed,
) -> None:
    settings, requests = _install_comparison_http(monkeypatch, _comparison_payload())

    class Pipeline:
        async def process_all(self, analysis_job_id, lease_token):
            assert analysis_job_id == UUID(int=1) and lease_token == UUID(int=99)
            print("private diagnostic must not reach stdout")
            return CharacterFactComparisonRunResult(
                completed_count=0 if candidate_failed else 1,
                failed_count=1 if candidate_failed else 0,
                first_failure_code=AnalysisFailureCode.LLM_OUTPUT_TRUNCATED if candidate_failed else None,
            )

    class LifecycleProbe(evaluation._AuditedComparisonWorker):
        def _get_comparison_pipeline(self, payload):
            return Pipeline()

    assert LifecycleProbe.claim_next is CharacterFactComparisonWorker.claim_next
    assert LifecycleProbe.process_claimed is CharacterFactComparisonWorker.process_claimed
    monkeypatch.setattr(evaluation, "_AuditedComparisonWorker", LifecycleProbe)
    result = asyncio.run(evaluation.run_one_comparison_job(
        settings=settings, audit_dir=tmp_path / "worker",
        expected_work_id=str(UUID(int=2)), expected_candidate_ids={UUID(int=5)},
    ))

    claim_request = json.loads(requests[0].content)
    assert claim_request["allowedJobTypes"] == ["CHARACTER_FACT_COMPARISON"]
    assert claim_request["modelName"] == "gpt-5.6-luna"
    assert result["claimed"] is True
    assert result["success"] is not candidate_failed
    assert result["jobCompleted"] is not candidate_failed
    assert result["settingCandidateId"] == str(UUID(int=5))
    assert result["episodeId"] is None  # Legacy hidden jobs may have no episode metadata.
    assert "leaseToken" not in result["claim"]
    assert requests[-1].url.path.endswith("/fail" if candidate_failed else "/complete")
    if candidate_failed:
        assert result["failureCode"] == "LLM_OUTPUT_TRUNCATED"
    else:
        assert result["summary"]["characterFactComparisonCompletedCount"] == 1
    audit_text = "".join(path.read_text() for path in (tmp_path / "worker").iterdir())
    for secret in ["internal-secret", "provider-secret", str(UUID(int=99))]:
        assert secret not in audit_text
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


@pytest.mark.parametrize(("change", "expected_ids", "reason"), [
    ({"jobType": "SETTING_EXTRACTION"}, {UUID(int=5)}, "WRONG_JOB_TYPE"),
    ({"settingCandidateId": None}, {UUID(int=5)}, "MISSING_CANDIDATE_ID"),
    ({"workId": str(UUID(int=20))}, {UUID(int=5)}, "WORK_MISMATCH"),
    ({"settingCandidateId": str(UUID(int=50))}, {UUID(int=5)}, "CANDIDATE_MISMATCH"),
    ({}, set(), "CANDIDATE_MISMATCH"),
])
def test_comparison_wrapper_blocks_out_of_scope_claim_before_processing(
    monkeypatch, tmp_path, change, expected_ids, reason,
) -> None:
    settings, requests = _install_comparison_http(monkeypatch, {**_comparison_payload(), **change})

    async def forbidden_process(*args):
        pytest.fail("A claim outside the requested review scope must not be processed.")

    monkeypatch.setattr(evaluation._AuditedComparisonWorker, "process_claimed", forbidden_process)
    result = asyncio.run(evaluation.run_one_comparison_job(
        settings=settings, audit_dir=tmp_path / "worker",
        expected_work_id=UUID(int=2), expected_candidate_ids=expected_ids,
    ))

    assert len(requests) == 1
    assert result["claimed"] is True and result["success"] is False
    assert result["failureCode"] == "UNEXPECTED_COMPARISON_CLAIM"
    assert result["guardReason"] == reason


def test_comparison_wrapper_records_empty_queue_without_processing(monkeypatch, tmp_path) -> None:
    settings, requests = _install_comparison_http(monkeypatch, None)
    result = asyncio.run(evaluation.run_one_comparison_job(
        settings=settings, audit_dir=tmp_path / "worker",
    ))
    assert len(requests) == 1
    assert result["claimed"] is False and result["success"] is False
    assert result["failureCode"] == "NO_CLAIMABLE_JOB"


def test_comparison_observer_keeps_production_pipeline_and_provider_ownership(
    monkeypatch, tmp_path,
) -> None:
    settings = _settings()
    monkeypatch.setattr("app.worker.character_fact_comparison_worker.get_settings", lambda: settings)
    provider = SimpleNamespace()
    monkeypatch.setattr(
        "app.worker.character_fact_comparison_worker.OpenAIResponsesClient.from_settings",
        lambda: provider,
    )
    worker = evaluation._AuditedComparisonWorker(
        audit=evaluation._Audit(tmp_path / "worker"), spring_client=SimpleNamespace(),
    )
    observed = worker._get_llm_provider_client()
    assert isinstance(observed, evaluation._AuditedProvider)
    assert observed.delegate is provider
    assert worker._llm_provider_client is provider
    assert worker._owns_llm_provider_client is True
    assert evaluation._AuditedComparisonWorker._get_comparison_pipeline is (
        CharacterFactComparisonWorker._get_comparison_pipeline
    )


def test_observation_review_audit_keeps_draft_and_final_candidates_separate(tmp_path):
    class Candidate:
        def __init__(self, active):
            self.active = active
            self._status_observation_kind = "START" if active else "END"
            self._status_observation_group = (UUID(int=1), 0)

        def model_dump(self, **kwargs):
            return {"attribute_name": "status.부상", "value_json": {"active": self.active}}

    onset = Candidate(True)
    final_result = SimpleNamespace(candidates=[onset, Candidate(False)])

    class Delegate:
        async def _review_status_observations(self, **kwargs):
            assert kwargs["statuses"].candidates == [onset]
            return final_result

        async def extract_from_chunk(self, **kwargs):
            return await self._review_status_observations(
                source_chunk_id=kwargs["source_chunk_id"],
                statuses=SimpleNamespace(candidates=[onset]),
            )

    audit = evaluation._Audit(tmp_path / "audit")
    observed = asyncio.run(evaluation._AuditedExtractor(Delegate(), audit).extract_from_chunk(
        source_chunk_id=UUID(int=1), episode_no=1, chunk_text="private source",
    ))
    assert observed is final_result
    before = next(audit.directory.glob('*status-observation-review-before.json'))
    after = next(audit.directory.glob('*status-observation-review-after.json'))
    assert len(json.loads(before.read_text())["candidates"]) == 1
    assert len(json.loads(after.read_text())["candidates"]) == 2
    assert json.loads(before.read_text())["candidates"][0]["value_json"]["active"] is True
    assert json.loads(before.read_text())["statusObservationKinds"] == ["START"]
    assert json.loads(after.read_text())["statusObservationKinds"] == ["START", "END"]
    assert json.loads(before.read_text())["priorStatusObservationKinds"] == []
    assert json.loads(before.read_text())["statusObservationGroups"] == [[str(UUID(int=1)), 0]]
    assert json.loads(after.read_text())["statusObservationGroups"] == [[str(UUID(int=1)), 0]] * 2
    assert "_status_observation_kind" not in json.dumps(json.loads(after.read_text())["candidates"])
    assert "private source" not in before.read_text() + after.read_text()
