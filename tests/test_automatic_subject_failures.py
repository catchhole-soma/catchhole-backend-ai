"""A bad optional identity judgment must not discard independent extracted facts."""

import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

import httpx
import pytest

from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.ordered_character_subjects import (
    OrderedSubjectResolutionMetrics, OrderedSubjectTarget, resolve_ordered_character_subjects,
)
from app.analysis.schemas import ExtractedSettingCandidate
from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.clients.exceptions import AiTokenQuotaExhaustedError, SpringWorkerHttpError, WorkerLeaseExpiredError
from app.domain.enums import AnalysisFailureCode
from app.llm.responses import LlmTextResponse
from app.schemas.worker import (
    WorkerWorldSettingSubjectPageResponse, WorkerWorldSettingSubjectResolutionCandidate,
    WorkerWorldSettingSubjectResolutionPendingResponse, WorkerWorldSettingSubjectResolutionResponse,
)
from tests.test_ordered_analysis_runtime import _input_context


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    class Encoding:
        def encode(self, text, **kwargs):
            return list(text.encode())
    monkeypatch.setattr("app.analysis.ordered_context.tiktoken.get_encoding", lambda *args: Encoding())
    monkeypatch.setattr("app.analysis.world_setting_comparator.get_settings", lambda: SimpleNamespace())


class Responses:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    async def create_text_response(self, **kwargs):
        self.calls.append(kwargs)
        item = self.outputs.pop(0)
        if isinstance(item, BaseException):
            raise item
        return LlmTextResponse(text=item if isinstance(item, str) else json.dumps(item))


def http_error(status, code="test_error", error_type=httpx.HTTPStatusError):
    request = httpx.Request("POST", "https://offline.invalid/responses")
    response = httpx.Response(status, request=request, json={"error": {"code": code}})
    return error_type("Synthetic failure", request=request, response=response)


class WorldSpring:
    def __init__(self, tamper_failure=False):
        self.context = _input_context()
        self.sources = [WorkerWorldSettingSubjectResolutionCandidate(
            candidate_id=uuid4(), source_episode_id=uuid4(), source_episode_no=18,
            category="IMPORTANT_ITEM", subject_name=name, evidence_spans=[{"quote": evidence}],
        ) for name, evidence in [
            ("마석", "마물에서 마석을 얻는다."), ("스톤", "스톤으로 가격을 표시한다."),
            ("마석", "마석을 모아 환전한다."),
        ]]
        self.saved, self.tamper_failure = [], tamper_failure

    async def get_pending_world_setting_subject_resolutions(self, *args):
        return WorkerWorldSettingSubjectResolutionPendingResponse(
            candidates=[] if self.saved else self.sources, analysis_context=self.context,
        )

    async def get_world_setting_subjects(self, *args):
        return WorkerWorldSettingSubjectPageResponse(subjects=[], page=0, has_next=False)

    async def complete_world_setting_subject_resolutions(self, job, lease, request):
        self.saved.append(request)
        results = []
        for row, source in zip(request.resolutions, self.sources):
            failed = row.failure_code is not None
            results.append({
                "candidate_id": row.candidate_id,
                "resolution_type": ("AMBIGUOUS" if self.tamper_failure else "FAILED") if failed else "NEW",
                "canonical_subject_key": f"failed:{row.candidate_id}" if failed else row.provisional_subject_keys[0],
                "canonical_subject_name": source.subject_name,
                "target_world_setting_ids": [], "provisional_subject_keys": row.provisional_subject_keys,
            })
        return WorkerWorldSettingSubjectResolutionResponse(resolutions=results)


def run_world(outputs, *, automatic=True, tamper_failure=False):
    client, spring = Responses(outputs), WorldSpring(tamper_failure)
    resolver = WorldSettingSubjectResolver(llm_client=client, model="offline", max_attempts=3,
                                          max_output_tokens=2000)
    pipeline = WorldSettingComparisonPipeline(spring, resolver, object())
    asyncio.run(pipeline._prepare_subject_resolutions(
        uuid4(), uuid4(), spring.context, continue_on_candidate_failure=automatic,
    ))
    return client, spring


@pytest.mark.parametrize(("invalid", "failure_code"), [
    ({"selected_subject_refs": ["S999"], "ambiguous": False}, "COMPARISON_VALIDATION_FAILED"),
    ({"selected_subject_refs": ["S1", "S1"], "ambiguous": False}, "COMPARISON_VALIDATION_FAILED"),
    ({"selected_subject_refs": []}, "LLM_RESPONSE_PARSE_ERROR"),
    ("{invalid", "LLM_RESPONSE_PARSE_ERROR"),
])
def test_automatic_world_failure_preserves_normal_neighbors_and_does_not_create_identity(invalid, failure_code):
    good = {"selected_subject_refs": ["S1"], "ambiguous": False}
    client, spring = run_world([invalid] * 3 + [good])
    first, failed, last = spring.saved[0].resolutions
    assert first.failure_code is None and last.failure_code is None
    assert failed.failure_code == failure_code and failed.ambiguous is False
    assert failed.target_world_setting_ids == failed.provisional_subject_keys == []
    assert first.provisional_subject_keys == last.provisional_subject_keys
    later = json.loads(client.calls[-1]["user_prompt"])
    assert len(later["subjects"]) == 1
    assert later["subjects"][0]["identity_evidence"] == [spring.sources[0].evidence_spans[0].quote]
    assert "스톤으로 가격" not in json.dumps(later["subjects"], ensure_ascii=False)
    assert len(client.calls) == 4


def test_world_failure_cannot_be_changed_to_normal_ambiguity_by_backend():
    invalid = {"selected_subject_refs": ["S999"], "ambiguous": False}
    good = {"selected_subject_refs": ["S1"], "ambiguous": False}
    with pytest.raises(ComparisonValidationError, match="changed a failed"):
        run_world([invalid] * 3 + [good], tamper_failure=True)


def test_manual_world_subject_failure_keeps_existing_stop_boundary():
    invalid = {"selected_subject_refs": ["S999"], "ambiguous": False}
    with pytest.raises(ComparisonValidationError):
        run_world([invalid] * 3, automatic=False)


def character_sources():
    chunk = uuid4()
    def setting(name):
        return ExtractedSettingCandidate(
            source_chunk_id=chunk, entity_name=name, attribute_name="level", attribute_value="12",
            value_type="NUMBER", value_json={"value": 12}, evidence_spans=[{"quote": f"{name}는 성장했다."}],
        )
    return [setting("비요른"), setting("미상"), ExtractedSettingCandidate(
        source_chunk_id=chunk, candidate_kind="CHARACTER_DISCOVERY", entity_name="세룸",
        evidence_spans=[{"quote": "세룸이 나타났다."}],
    )]


def run_characters(outputs, *, automatic=True):
    sources, client, metrics, known_id = character_sources(), Responses(outputs), OrderedSubjectResolutionMetrics(), uuid4()
    before = [item.model_dump() for item in sources]
    resolved, discoveries = asyncio.run(resolve_ordered_character_subjects(
        client=client, model="offline", max_attempts=3, max_output_tokens=2000,
        context=SubjectResolutionChunkContext("세룸과 비요른이 만났다.", "그는 성장했다.", None),
        candidates=sources, subjects=[OrderedSubjectTarget(name="비요른", actual_character_id=known_id)],
        episode_no=18, metrics=metrics, continue_on_candidate_failure=automatic,
    ))
    assert [item.model_dump() for item in sources] == before
    return resolved, discoveries, client, metrics, known_id


@pytest.mark.parametrize(("invalid", "failure_code"), [
    ({"resolutions": [{"candidate_ref": "C2", "target_ref": "K999"}]}, "COMPARISON_VALIDATION_FAILED"),
    ({"resolutions": []}, "COMPARISON_VALIDATION_FAILED"),
    ({"resolutions": [{"candidate_ref": "C2"}]}, "LLM_RESPONSE_PARSE_ERROR"),
    ("{invalid", "LLM_RESPONSE_PARSE_ERROR"),
])
def test_character_enhancement_failure_keeps_rule_bindings_and_discoveries(invalid, failure_code):
    resolved, discoveries, client, metrics, known_id = run_characters([invalid] * 3)
    normal, failed, discovery = [item.binding for item in resolved]
    assert normal.actual_character_id == known_id and normal.subject_failure_code is None
    assert failed.subject_failure_code == failure_code and failed.match_status == "AMBIGUOUS"
    assert failed.actual_character_id is None and failed.provisional_subject_key is None
    assert discovery.provisional_subject_key and discovery.subject_failure_code is None
    assert len(discoveries) == 1 and discoveries[0].name == "세룸"
    assert metrics.llm_failed_count == 1 and metrics.llm_unresolved_count == 0
    assert len(client.calls) == 3


def test_manual_character_enhancement_keeps_existing_stop_boundary():
    with pytest.raises(ComparisonValidationError):
        run_characters([{"resolutions": []}] * 3, automatic=False)


@pytest.mark.parametrize("make_error", [
    lambda: AiTokenQuotaExhaustedError(),
    lambda: http_error(409, error_type=WorkerLeaseExpiredError),
    lambda: OrderedInputContextError("fixed input changed"),
    lambda: http_error(500, error_type=SpringWorkerHttpError),
    lambda: http_error(401),
    lambda: http_error(403),
    lambda: http_error(429, "insufficient_quota"),
    lambda: RuntimeError("unexpected implementation failure"),
])
@pytest.mark.parametrize("run", [run_world, run_characters])
def test_automatic_subject_resolution_does_not_hide_execution_or_account_failures(make_error, run):
    error = make_error()
    with pytest.raises(type(error)):
        run([error])


def test_character_subject_failure_is_persisted_separately_from_ai_ambiguity(monkeypatch):
    from app.services.setting_candidate_service import SettingCandidateSaveItem, SettingCandidateService
    from tests.test_setting_candidate_service import FakeSession, FakeSettingCandidateRepository

    resolved, _, _, _, _ = run_characters([{"resolutions": []}] * 3)
    session = FakeSession()
    repository = FakeSettingCandidateRepository(session)
    service = SettingCandidateService(lambda: session, lambda unused: repository)
    monkeypatch.setattr("app.services.setting_candidate_service.fence_ordered_candidate_write", lambda *a, **k: None)
    saved = service.replace_candidates_for_analysis_job(
        work_id=uuid4(), analysis_job_id=uuid4(), known_characters=[], ordered_write_context=object(),
        save_items=[SettingCandidateSaveItem(uuid4(), "local/source", item.candidate, item.binding)
                    for item in resolved],
    )
    failed = saved[1]
    assert failed.comparison_status == "FAILED"
    assert failed.comparison_failure_code == AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    assert failed.preparation_failure_stage == "SUBJECT_RESOLUTION"
    assert failed.raw_ai_result_json["orderedSubjectResolution"]["failureStage"] == "SUBJECT_RESOLUTION"
    assert failed.automatic_review_hold_reason is None  # Spring owns the user-facing hold reason.
    assert saved[0].comparison_status == "PENDING" and saved[2].comparison_status == "NOT_REQUIRED"
