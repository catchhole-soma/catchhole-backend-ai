"""Grounded separation annotations never authorize identity binding."""
import hashlib
import json
from pathlib import Path

import pytest

from app.analysis.exceptions import ComparisonValidationError
from tests.test_book_world_identity_proofs import choice, run, source_candidate, target
from tests.test_ordered_subject_rules import no_network as no_network

RECORDED = json.loads((Path(__file__).parent / "fixtures/book_world_nonselected_proofs.json").read_text())


@pytest.mark.parametrize("packet", RECORDED, ids=lambda p: p["id"])
def test_recorded_separation_explanations_preserve_only_explicit_selected_targets(packet):
    data = packet["user"]
    item = source_candidate(data["candidate"]["subject_name"], data["candidate"]["evidence"])
    subjects = [target(s["name"], s["identity_evidence"], s["aliases"]) for s in data["subjects"]]
    before = [s.model_dump() for s in subjects]
    assert hashlib.sha256(packet["raw"].encode()).hexdigest() == packet["raw_sha256"]
    raw = json.loads(packet["raw"])
    (selected, ambiguous), client = run(item, subjects, [raw], attempts=1)
    assert [s.subject_name for s in selected] == (["조율판"] if "S3" in raw["selected_subject_refs"] else [])
    assert not ambiguous and len(client.requests) == 1
    assert [s.model_dump() for s in subjects] == before


@pytest.mark.parametrize("ambiguous", [False, True])
@pytest.mark.parametrize("bad", [
    {"subject_ref": "S9", "relation": "DISTINCT", "quote": "별도 시설이다."},
    {"subject_ref": "S1", "relation": "SAME_ENTITY", "quote": "별도 시설이다."},
    {"subject_ref": "S1", "relation": "UNKNOWN", "quote": "별도 시설이다."},
    {"subject_ref": "S1", "relation": "CONTAINS", "quote": "별도 시설이다."},
    {"subject_ref": "S1", "relation": "DISTINCT", "quote": "입력에 없는 문장"},
    {"subject_ref": "S1", "relation": "RELATED", "quote": "다른 대상의 전용 근거"},
])
def test_nonselected_annotations_still_reject_unknown_unproven_or_identity_claims(bad, ambiguous):
    item = source_candidate("새문", ["별도 시설이다."])
    subjects = [target("동문", ["일출 개방"]), target("남문", ["다른 대상의 전용 근거"])]
    with pytest.raises(ComparisonValidationError):
        run(item, subjects, [choice(proofs=[bad], ambiguous=ambiguous)], attempts=1)


def test_duplicate_nonselected_explanations_remain_invalid():
    item = source_candidate("새문", ["동문과 별도 시설이다."])
    subject = target("동문", [])
    annotation = {"subject_ref": "S1", "relation": "DISTINCT", "quote": "동문과 별도 시설이다."}
    with pytest.raises(ComparisonValidationError):
        run(item, [subject], [choice(proofs=[annotation, annotation])], attempts=1)


def test_pipeline_enriches_selected_identity_without_enriching_explanation_target():
    import asyncio
    from uuid import uuid4
    from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
    from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
    from app.schemas.worker import (WorkerAnalysisJobPayload, WorkerWorldSettingSubjectPageResponse,
        WorkerWorldSettingSubjectResolutionPendingResponse, WorkerWorldSettingSubjectResolutionResponse)
    from tests.test_ordered_analysis_runtime import RecordingClient
    from tests.test_setting_book_source_contract import book_payload_data

    context = WorkerAnalysisJobPayload.model_validate(book_payload_data()).analysis_context
    quote = "조율판은 측정봉과 다른 도구다."
    first = source_candidate("조율판", [quote])
    second = source_candidate("측정봉", ["측정봉은 길이를 잰다."])
    plate = target("조율판", ["조율판은 금속판이다."])
    rod = target("측정봉", ["측정봉은 공구다."])
    original = [s.model_dump() for s in [plate, rod]]
    saved = []

    class Spring:
        async def get_pending_world_setting_subject_resolutions(self, *_):
            return WorkerWorldSettingSubjectResolutionPendingResponse(candidates=[] if saved else [first, second], analysis_context=context)
        async def get_world_setting_subjects(self, *_):
            return WorkerWorldSettingSubjectPageResponse(subjects=[plate, rod], page=0, has_next=False)
        async def complete_world_setting_subject_resolutions(self, _job, _lease, request):
            saved.append(request)
            return WorkerWorldSettingSubjectResolutionResponse(resolutions=[{
                "candidateId": row.candidate_id, "resolutionType": "EXISTING",
                "canonicalSubjectKey": row.provisional_subject_keys[0],
                "canonicalSubjectName": "조율판" if row.candidate_id == first.candidate_id else "측정봉",
                "targetWorldSettingIds": [], "provisionalSubjectKeys": row.provisional_subject_keys,
            } for row in request.resolutions])

    client = RecordingClient(choice(["S1"], [{"subject_ref": "S2", "relation": "DISTINCT", "quote": quote}]), choice(["S2"]))
    asyncio.run(WorldSettingComparisonPipeline(Spring(), WorldSettingSubjectResolver(llm_client=client, model="test-model", max_attempts=1), object())._prepare_subject_resolutions(uuid4(), uuid4(), context, analysis_source=first.source))
    rows = saved[0].resolutions
    assert [r.provisional_subject_keys for r in rows] == [[plate.provisional_subject_key], [rod.provisional_subject_key]]
    next_subjects = json.loads(client.requests[1]["user_prompt"])["subjects"]
    assert next_subjects[0]["identity_evidence"] == ["조율판은 금속판이다.", quote]
    assert next_subjects[1]["identity_evidence"] == ["측정봉은 공구다."]
    assert [s.model_dump() for s in [plate, rod]] == original
