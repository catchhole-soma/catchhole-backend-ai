"""Character-only evaluation around the unchanged production Worker lifecycle.

The caller configures the process environment before obtaining ``get_settings()``
and runs one job at a time. Audit files contain private novel-derived candidates;
they never include authentication headers, the lease token, or model prompts.
"""

import json
import os
from collections.abc import Collection
from contextlib import redirect_stderr, redirect_stdout
from datetime import date, datetime
from decimal import Decimal
from io import StringIO
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import urlsplit
from uuid import UUID

import httpx
from sqlalchemy.engine import make_url

from app.analysis.world_setting_schemas import WorldSettingExtractionResult
from app.clients.spring_worker_client import SpringWorkerClient
from app.core.config import Settings, get_settings
from app.db.session import get_session_maker
from app.domain.enums import AnalysisJobType
from app.exceptions.failure_classification import analysis_failure_code
from app.services.episode_chunk_service import EpisodeChunkService
from app.services.episode_s3_chunking_service import EpisodeS3ChunkingService
from app.services.setting_candidate_service import SettingCandidateService
from app.worker.analysis_job_worker import AnalysisJobWorker
from app.worker.character_fact_comparison_worker import CharacterFactComparisonWorker
from evals.setting_extraction.run_gold_analysis import _disable_logging

_AUTH_KEYS = {
    "authorization",
    "cookie",
    "setcookie",
    "leasetoken",
    "xworkerleasetoken",
    "internalapikey",
    "xinternalapikey",
    "llmapikey",
    "apikey",
    "accesstoken",
    "refreshtoken",
    "password",
    "secret",
}
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def _without_auth(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _without_auth(item)
            for key, item in value.items()
            if str(key).lower().replace("_", "").replace("-", "") not in _AUTH_KEYS
        }
    if isinstance(value, list):
        return [_without_auth(item) for item in value]
    return value


def _json_default(value: Any) -> str:
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    if isinstance(value, (UUID, Decimal)):
        return str(value)
    raise TypeError(f"Unsupported audit value type: {type(value).__name__}")


class _Audit:
    def __init__(self, directory: Path) -> None:
        self.directory = directory
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if any(directory.iterdir()):
            raise ValueError("Worker audit directory must be empty; preserve prior runs.")
        self._lock = Lock()
        self.count = 0

    def write(self, kind: str, payload: Any) -> None:
        with self._lock:
            self.count += 1
            path = self.directory / f"{self.count:04d}-{kind}.json"
            descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(descriptor, "w", encoding="utf-8") as output:
                json.dump(
                    _without_auth(payload),
                    output,
                    ensure_ascii=False,
                    indent=2,
                    default=_json_default,
                )
                output.write("\n")


class LocalSourceStorage:
    """Read the object actually uploaded through Java's e2e storage adapter."""

    def __init__(self, storage_root: Path) -> None:
        self.storage_root = storage_root.resolve()

    def get_text(self, key: str) -> str:
        source = (self.storage_root / key).resolve()
        if not source.is_relative_to(self.storage_root) or Path(key).is_absolute():
            raise ValueError("Source object key escapes the local storage root.")
        # read_bytes avoids translating CRLF and changing production evidence offsets.
        return source.read_bytes().decode("utf-8")


class _EmptyWorldExtractor:
    async def extract_from_chunk(self, **kwargs: Any) -> WorldSettingExtractionResult:
        return WorldSettingExtractionResult(candidates=[])


class _AuditedSpringClient(SpringWorkerClient):
    def __init__(self, *, audit: _Audit, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.audit = audit
        self.summary: dict[str, Any] = {}
        self.failure_code: str | None = None

    async def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        # Headers are intentionally never observed. All calls still use the real client,
        # including token reserve/settle, heartbeat, comparison context and completion.
        path = urlsplit(url).path
        self.audit.write(
            "spring-request",
            {
                "method": method,
                "path": path,
                "body": kwargs.get("json"),
            },
        )
        try:
            response = await super()._request(method, url, **kwargs)
        except Exception as exc:
            self.audit.write(
                "spring-transport-failure",
                {
                    "method": method,
                    "path": path,
                    "exceptionType": type(exc).__name__,
                },
            )
            raise
        try:
            body = response.json() if response.content else None
        except ValueError:
            body = {"nonJsonResponse": True}
        self.audit.write(
            "spring-response",
            {
                "method": method,
                "path": path,
                "status": response.status_code,
                "body": body,
            },
        )
        return response

    async def complete(self, *args: Any, **kwargs: Any) -> None:
        summary_json = kwargs.get("summary_json")
        await super().complete(*args, **kwargs)
        if summary_json is not None:
            self.summary = json.loads(summary_json)

    async def fail(self, *args: Any, **kwargs: Any) -> None:
        code = kwargs.get("failure_code", args[3] if len(args) > 3 else None)
        if code is not None:
            self.failure_code = code.value if hasattr(code, "value") else str(code)
        await super().fail(*args, **kwargs)


class _AuditedExtractor:
    def __init__(self, delegate: Any, audit: _Audit) -> None:
        self.delegate = delegate
        self.audit = audit
        if hasattr(delegate, "_extract_with_retry"):
            original = delegate._extract_with_retry

            async def audited_pass(**kwargs: Any) -> Any:
                evidence_document = kwargs.get("evidence_document")
                if evidence_document is not None:
                    audit.write(
                        "status-evidence-source",
                        {
                            "sourceChunkId": kwargs["source_chunk_id"],
                            "units": [
                                {
                                    "ref": unit.ref,
                                    "text": unit.text,
                                    "startOffset": unit.start_offset,
                                    "endOffset": unit.end_offset,
                                }
                                for unit in evidence_document.units
                            ],
                        },
                    )
                result = await original(**kwargs)
                audit.write(
                    "status-reviewed-candidates" if kwargs.get("status_only")
                    else "general-draft-candidates",
                    {
                        "candidates": [item.model_dump(mode="json") for item in result.candidates],
                        "statusObservationKinds": [getattr(item, "_status_observation_kind", None) for item in result.candidates],
                        "statusObservationGroups": [getattr(item, "_status_observation_group", None) for item in result.candidates],
                    },
                )
                return result

            delegate._extract_with_retry = audited_pass

        if hasattr(delegate, "_review_status_observations"):
            original_review = delegate._review_status_observations

            async def audited_observation_review(**kwargs: Any) -> Any:
                audit.write(
                    "status-observation-review-before",
                    {
                        "sourceChunkId": kwargs["source_chunk_id"],
                        "candidates": [item.model_dump(mode="json")
                                       for item in kwargs["statuses"].candidates],
                        "statusObservationKinds": [getattr(item, "_status_observation_kind", None)
                                                   for item in kwargs["statuses"].candidates],
                        "statusObservationGroups": [getattr(item, "_status_observation_group", None)
                                                    for item in kwargs["statuses"].candidates],
                        "priorStatusObservations": [item.model_dump(mode="json")
                                                    for item in kwargs.get("prior_status_observations", ())],
                        "priorStatusObservationKinds": [getattr(item, "_status_observation_kind", None)
                                                        for item in kwargs.get("prior_status_observations", ())],
                        "priorStatusObservationGroups": [getattr(item, "_status_observation_group", None)
                                                         for item in kwargs.get("prior_status_observations", ())],
                    },
                )
                result = await original_review(**kwargs)
                audit.write(
                    "status-observation-review-after",
                    {
                        "sourceChunkId": kwargs["source_chunk_id"],
                        "candidates": [item.model_dump(mode="json")
                                       for item in result.candidates],
                        "statusObservationKinds": [getattr(item, "_status_observation_kind", None) for item in result.candidates],
                        "statusObservationGroups": [getattr(item, "_status_observation_group", None) for item in result.candidates],
                    },
                )
                return result

            delegate._review_status_observations = audited_observation_review

    async def extract_from_chunk(self, **kwargs: Any) -> Any:
        result = await self.delegate.extract_from_chunk(**kwargs)
        self.audit.write(
            "extracted-candidates",
            {
                "sourceChunkId": kwargs["source_chunk_id"],
                "episodeNo": kwargs.get("episode_no"),
                "candidates": [item.model_dump(mode="json") for item in result.candidates],
                "statusObservationKinds": [getattr(item, "_status_observation_kind", None) for item in result.candidates],
                "statusObservationGroups": [getattr(item, "_status_observation_group", None) for item in result.candidates],
            },
        )
        return result


class _AuditedCandidateService(SettingCandidateService):
    def __init__(self, *, audit: _Audit, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.audit = audit

    def replace_candidates_for_analysis_job(self, **kwargs: Any) -> Any:
        self.audit.write(
            "candidates-before-dedup",
            {
                "analysisJobId": kwargs["analysis_job_id"],
                "candidates": [
                    item.candidate.model_dump(mode="json") for item in kwargs["save_items"]
                ],
            },
        )
        saved = super().replace_candidates_for_analysis_job(**kwargs)
        self.audit.write(
            "saved-candidates",
            {
                "analysisJobId": kwargs["analysis_job_id"],
                "candidates": [
                    {column.key: getattr(item, column.key) for column in item.__table__.columns}
                    for item in saved
                ],
            },
        )
        return saved


class _AuditedProvider:
    def __init__(self, delegate: Any, audit: _Audit) -> None:
        self.delegate = delegate
        self.audit = audit

    async def create_text_response(self, **kwargs: Any) -> Any:
        response = await self.delegate.create_text_response(**kwargs)
        # Persist response data, not prompts or client/header configuration. This
        # retains invalid batch responses needed to explain singleton fallback.
        try:
            payload = json.loads(response.text)
        except ValueError:
            payload = {"unparsedText": response.text}
        self.audit.write("provider-response", {
            "model": kwargs.get("model"),
            "promptCacheKey": kwargs.get("prompt_cache_key"),
            "response": payload,
        })
        return response


class _AuditedWorker(AnalysisJobWorker):
    def __init__(self, *, audit: _Audit, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.audit = audit

    def _get_llm_provider_client(self) -> Any:
        return _AuditedProvider(super()._get_llm_provider_client(), self.audit)

    def _get_setting_extractor(self, analysis_job_id: UUID, lease_token: UUID) -> Any:
        return _AuditedExtractor(
            super()._get_setting_extractor(analysis_job_id, lease_token),
            self.audit,
        )


class _AuditedComparisonWorker(CharacterFactComparisonWorker):
    def __init__(self, *, audit: _Audit, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.audit = audit

    def _get_llm_provider_client(self) -> Any:
        return _AuditedProvider(super()._get_llm_provider_client(), self.audit)


def _validate_runtime(settings: Settings) -> None:
    if settings != get_settings():
        raise ValueError(
            "Pass get_settings() after configuring the evaluation process environment."
        )
    if urlsplit(settings.spring_internal_api_base_url).hostname not in _LOOPBACK_HOSTS:
        raise ValueError("Stability evaluation requires a localhost Spring API.")
    if make_url(settings.database_url).host not in _LOOPBACK_HOSTS:
        raise ValueError("Stability evaluation requires a localhost database.")


async def run_one_job(*, settings: Settings, storage_root: Path, audit_dir: Path) -> dict:
    """Claim and execute one real job; the caller performs user confirmation afterward.

    ``success`` means Worker completion with no typed character comparison failures,
    not semantic correctness. The caller judges the actual confirmed snapshot.
    This serial evaluation suppresses global logging while private data is processed.
    """
    _validate_runtime(settings)
    audit = _Audit(audit_dir)
    metadata = {
        "extractionModel": settings.effective_llm_extraction_model,
        "subjectResolutionModel": settings.effective_llm_subject_resolution_model,
        "comparisonModel": settings.effective_llm_comparison_model,
        "reasoningEffort": settings.llm_reasoning_effort,
        "parity": {
            "characterProductionPipeline": True,
            "storage": "java-e2e-uploaded-local-object",
            "worldExtraction": "omitted-empty-candidates",
            "embeddingGeneration": False,
            "confirmation": "caller-must-use-java-api-before-next-episode",
            "execution": "one-claimed-job-at-a-time",
        },
    }
    audit.write("runtime", metadata)
    spring = _AuditedSpringClient(
        audit=audit,
        base_url=settings.spring_internal_api_base_url,
        internal_api_key=settings.spring_internal_api_key,
    )
    chunks = EpisodeChunkService(session_factory=get_session_maker())
    worker = _AuditedWorker(
        audit=audit,
        spring_client=spring,
        chunking_service=EpisodeS3ChunkingService(
            storage=LocalSourceStorage(storage_root),
            chunk_service=chunks,
        ),
        episode_chunk_service=chunks,
        setting_candidate_service=_AuditedCandidateService(
            audit=audit,
            session_factory=get_session_maker(),
        ),
        world_setting_extractor=_EmptyWorldExtractor(),
        embedding_generation_enabled=False,
    )
    result: dict[str, Any] = {
        "claimed": False,
        "success": False,
        "jobCompleted": False,
        "analysisJobId": None,
        "episodeId": None,
        "claim": None,
        "summary": {},
        **metadata,
    }
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()), _disable_logging():
        try:
            payload = await worker.claim_next()
            if payload is None:
                result["failureCode"] = "NO_CLAIMABLE_JOB"
            else:
                result.update(
                    {
                        "claimed": True,
                        "analysisJobId": str(payload.analysis_job_id),
                        "episodeId": str(payload.episode.episode_id) if payload.episode else None,
                        "claim": _without_auth(payload.model_dump(by_alias=True, mode="json")),
                    }
                )
                await worker.process_claimed(payload)
                result.update(
                    {
                        "jobCompleted": True,
                        "success": spring.summary.get("characterFactComparisonFailedCount", 0) == 0,
                        "summary": spring.summary,
                    }
                )
        except Exception as exc:  # noqa: BLE001 - record each failed evaluation attempt
            result.update(
                {
                    "failureCode": analysis_failure_code(exc).value,
                    "exceptionType": type(exc).__name__,
                }
            )
        finally:
            try:
                await worker.aclose()
            finally:
                await spring.aclose()
    audit.write("worker-result", result)
    result["auditFileCount"] = audit.count
    return result


async def run_one_comparison_job(
    *,
    settings: Settings,
    audit_dir: Path,
    expected_work_id: UUID | str | None = None,
    expected_candidate_ids: Collection[UUID | str] | None = None,
) -> dict:
    """Execute one real character recomparison job within the caller's review scope.

    The production worker owns claim type, heartbeat, comparison, metering and
    completion. A mismatched claim is recorded but never processed or failed on
    another work's behalf; the caller must stop and inspect that isolated queue.
    """
    _validate_runtime(settings)
    expected_work = UUID(str(expected_work_id)) if expected_work_id is not None else None
    expected_candidates = (
        {UUID(str(candidate_id)) for candidate_id in expected_candidate_ids}
        if expected_candidate_ids is not None else None
    )
    audit = _Audit(audit_dir)
    metadata = {
        "comparisonModel": settings.effective_llm_comparison_model,
        "reasoningEffort": settings.llm_reasoning_effort,
        "parity": {
            "characterProductionPipeline": True,
            "jobType": AnalysisJobType.CHARACTER_FACT_COMPARISON.value,
            "execution": "one-claimed-job-at-a-time",
            "confirmation": "caller-must-use-java-api-after-recomparison",
        },
    }
    audit.write("runtime", metadata)
    spring = _AuditedSpringClient(
        audit=audit,
        base_url=settings.spring_internal_api_base_url,
        internal_api_key=settings.spring_internal_api_key,
    )
    worker = _AuditedComparisonWorker(audit=audit, spring_client=spring)
    result: dict[str, Any] = {
        "claimed": False,
        "success": False,
        "jobCompleted": False,
        "analysisJobId": None,
        "episodeId": None,
        "settingCandidateId": None,
        "claim": None,
        "summary": {},
        **metadata,
    }
    with redirect_stdout(StringIO()), redirect_stderr(StringIO()), _disable_logging():
        try:
            payload = await worker.claim_next()
            if payload is None:
                result["failureCode"] = "NO_CLAIMABLE_JOB"
            else:
                result.update({
                    "claimed": True,
                    "analysisJobId": str(payload.analysis_job_id),
                    "episodeId": str(payload.episode.episode_id) if payload.episode else None,
                    "settingCandidateId": (
                        str(payload.setting_candidate_id) if payload.setting_candidate_id else None
                    ),
                    "claim": _without_auth(payload.model_dump(by_alias=True, mode="json")),
                })
                guard_reason = None
                if payload.job_type != AnalysisJobType.CHARACTER_FACT_COMPARISON:
                    guard_reason = "WRONG_JOB_TYPE"
                elif payload.setting_candidate_id is None:
                    guard_reason = "MISSING_CANDIDATE_ID"
                elif expected_work is not None and payload.work_id != expected_work:
                    guard_reason = "WORK_MISMATCH"
                elif (expected_candidates is not None
                      and payload.setting_candidate_id not in expected_candidates):
                    guard_reason = "CANDIDATE_MISMATCH"
                if guard_reason is not None:
                    result.update({
                        "failureCode": "UNEXPECTED_COMPARISON_CLAIM",
                        "guardReason": guard_reason,
                    })
                else:
                    await worker.process_claimed(payload)
                    result.update({
                        "jobCompleted": True,
                        "success": spring.summary.get("characterFactComparisonFailedCount", 0) == 0,
                        "summary": spring.summary,
                    })
        except Exception as exc:  # noqa: BLE001 - preserve every failed evaluation attempt
            result.update({
                "failureCode": spring.failure_code or analysis_failure_code(exc).value,
                "exceptionType": type(exc).__name__,
            })
        finally:
            try:
                await worker.aclose()
            finally:
                await spring.aclose()
    audit.write("worker-result", result)
    result["auditFileCount"] = audit.count
    return result
