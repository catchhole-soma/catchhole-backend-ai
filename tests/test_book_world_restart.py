"""Persisted BOOK raw carriers survive restart and never create unresolved identities."""

import asyncio
import json
import os
from pathlib import Path
from uuid import uuid4

import pytest

from app.analysis.book_world_claims import is_asserted_book_claim
from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import (
    WorkerAnalysisJobPayload, WorkerWorldSettingCandidatePayload, WorkerWorldSettingSubject,
    WorkerWorldSettingSubjectPageResponse, WorkerWorldSettingSubjectResolutionCandidate,
    WorkerWorldSettingSubjectResolutionPendingResponse, WorkerWorldSettingSubjectResolutionResponse,
)
from tests.test_book_world_claims import candidate
from tests.test_ordered_analysis_runtime import RecordingClient
from tests.test_ordered_subject_rules import no_network as no_network
from tests.test_setting_book_source_contract import SOURCE, book_payload_data


@pytest.mark.parametrize("raw", [None, {}, {"epistemic_status": "UNRESOLVED_CLAIM"},
    {"epistemic_status": "ASSERTED", "sourceCandidates": []},
    {"epistemic_status": "ASSERTED", "sourceCandidates": {"epistemic_status": "ASSERTED"}},
    {"epistemic_status": "ASSERTED", "sourceCandidates": [
        {"epistemic_status": "ASSERTED", "sourceCandidates": [{"epistemic_status": "ASSERTED"}, {}]}]},
])
@pytest.mark.parametrize("ordered", [True, False])
def test_restart_unresolved_never_adds_an_anchor_or_evidence_even_with_exact_existing_name(raw, ordered):
    source = WorkerAnalysisSource.model_validate(SOURCE)
    context = WorkerAnalysisJobPayload.model_validate(book_payload_data()).analysis_context if ordered else None
    values = [raw, {"epistemic_status": "ASSERTED"}]
    items = [WorkerWorldSettingSubjectResolutionCandidate(candidate_id=uuid4(), source=source,
        category="WORLD_RULE_HISTORY", subject_name="검은 깃", rawExtractionJson=value,
        evidence_spans=[{"quote": "미확인 주장이 남아 있다." if index == 0 else "색은 붉다."}])
        for index, value in enumerate(values)]
    # Simulate the actual internal HTTP response/restart boundary, not shared Python object memory.
    pending = WorkerWorldSettingSubjectResolutionPendingResponse.model_validate_json(
        WorkerWorldSettingSubjectResolutionPendingResponse(candidates=items, analysis_context=context).model_dump_json(by_alias=True))
    before = pending.model_dump()
    subject = WorkerWorldSettingSubject(world_setting_id=uuid4(), subject_name="검은 깃",
        identity_evidence=[{"quote": "기원은 고대 의식이다."}])
    saved = []
    class Spring:
        async def get_pending_world_setting_subject_resolutions(self, *_args):
            return pending if not saved else WorkerWorldSettingSubjectResolutionPendingResponse(candidates=[], analysis_context=context)
        async def get_world_setting_subjects(self, *_args):
            return WorkerWorldSettingSubjectPageResponse(subjects=[subject], page=0, has_next=False)
        async def complete_world_setting_subject_resolutions(self, _job, _lease, request):
            saved.append(request)
            return WorkerWorldSettingSubjectResolutionResponse(resolutions=[{
                "candidateId": row.candidate_id, "resolutionType": "AMBIGUOUS" if row.ambiguous else "EXISTING",
                "canonicalSubjectKey": "pending" if row.ambiguous else "existing",
                "canonicalSubjectName": "검은 깃", "targetWorldSettingIds": row.target_world_setting_ids,
                "provisionalSubjectKeys": row.provisional_subject_keys,
            } for row in request.resolutions])
    client = RecordingClient({"decisions": [{"candidate_ref": "C2", "selected_subject_refs": ["S1"],
                                            "ambiguous": False, "identity_proofs": []}]})
    asyncio.run(WorldSettingComparisonPipeline(Spring(),
        WorldSettingSubjectResolver(llm_client=client, model="test-model"), object())._prepare_subject_resolutions(
            uuid4(), uuid4(), context, analysis_source=source))
    first, asserted = saved[0].resolutions
    assert first.ambiguous and first.target_world_setting_ids == [] and first.provisional_subject_keys == []
    assert not asserted.ambiguous and asserted.target_world_setting_ids == [subject.world_setting_id]
    assert pending.model_dump() == before
    if ordered:
        assert len(client.requests) == 1
        assert json.loads(client.requests[0]["user_prompt"])["initial_subjects"][0]["identity_evidence"] == ["기원은 고대 의식이다."]
    else:
        assert client.requests == []


def test_raw_claim_carrier_roundtrip_is_identical_for_all_three_internal_world_payloads():
    raw = {"epistemic_status": "ASSERTED", "sourceCandidates": [{"epistemic_status": "UNRESOLVED_CLAIM"}],
           "userValue": {"sourceCandidates": [{"epistemic_status": "ASSERTED"}]}}
    source = WorkerAnalysisSource.model_validate(SOURCE)
    values = [candidate(raw),
        WorkerWorldSettingSubjectResolutionCandidate(candidate_id=uuid4(), category="WORLD_RULE_HISTORY",
            subject_name="기록", source=source, evidence_spans=[{"quote": "미확인"}], rawExtractionJson=raw),
        WorkerWorldSettingCandidatePayload(candidate_id=uuid4(), work_id=uuid4(), category="WORLD_RULE_HISTORY",
            subject_name="기록", source=source, setting_name="주장", extracted_value="미확인",
            evidence_spans=[{"quote": "미확인"}], rawExtractionJson=raw)]
    for value in values:
        wire = value.model_dump(mode="json", by_alias=True)
        assert wire["rawExtractionJson"] == raw
        restored = type(value).model_validate_json(json.dumps(wire))
        assert restored.raw_extraction_json == raw and not is_asserted_book_claim(restored.raw_extraction_json)


def test_only_designated_source_candidates_edges_control_the_claim():
    assert is_asserted_book_claim({"epistemic_status": "ASSERTED", "sourceCandidates": [
        {"epistemic_status": "ASSERTED", "value": {"epistemic_status": "UNRESOLVED_CLAIM", "sourceCandidates": []}}]})


ASSERTION_WIRE_PATH = os.environ.get("GH221_JAVA_WORLD_ASSERTION_WIRE")


@pytest.mark.skipif(not ASSERTION_WIRE_PATH, reason="Actual Java BOOK world assertion-carrier HTTP export is not supplied.")
def test_actual_java_world_assertion_carrier_is_preserved_at_every_worker_boundary():
    from tests.test_java_worker_conformance import parse_java_worker_conformance
    raw_export = json.loads(Path(ASSERTION_WIRE_PATH).read_text(encoding="utf-8"))
    parsed = parse_java_worker_conformance(raw_export)
    for candidate, raw in (
        (parsed["worldCandidates"][0], raw_export["worldCandidates"][0]["rawExtractionJson"]),
        (parsed["worldSubjectPending"].candidates[0], raw_export["worldSubjectPending"]["candidates"][0]["rawExtractionJson"]),
        (parsed["worldBatch"].candidates[0], raw_export["worldBatch"]["candidates"][0]["rawExtractionJson"]),
    ):
        assert candidate.raw_extraction_json == raw
        assert raw["epistemic_status"] == "ASSERTED" and raw["sourceCandidates"]
        assert is_asserted_book_claim(raw)
        assert candidate.model_dump(mode="json", by_alias=True)["rawExtractionJson"] == raw
