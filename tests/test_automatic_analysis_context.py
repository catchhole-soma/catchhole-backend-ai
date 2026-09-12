"""Offline evidence that saved facts and unresolved claims remain distinct LLM inputs."""

import asyncio
import json
import socket
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.analysis.character_fact_comparison_pipeline import execute_character_fact_comparison_batch
from app.analysis.character_fact_comparator import CharacterFactComparator, _estimate_prompt_tokens
from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_character_subjects import (
    OrderedSubjectTarget, initial_ordered_subjects, merge_ordered_subjects,
    resolve_ordered_character_subjects,
)
from app.analysis.ordered_context import OrderedExtractionContext
from app.analysis.schemas import ExtractedSettingCandidate
from app.analysis.setting_extractor import CharacterSettingExtractor
from app.analysis.world_setting_comparator import WorldSettingComparator, WorldSettingSubjectResolver
from app.analysis.world_setting_extractor import WorldSettingExtractor
from app.schemas.analysis_context import AnalysisStateProvenance, WorkerAnalysisReference
from app.schemas.worker import (
    WorkerAnalysisJobPayload, WorkerAnalysisKnownCharacterPayload,
    WorkerWorldSettingSubjectResolutionCandidate,
)
from tests.test_character_fact_batch import _batch_snapshot, _candidate, _decision_payload
from tests.test_ordered_analysis_runtime import (
    RecordingClient, _input_context, _ordered_payload, _temporary_character,
)
from tests.test_ordered_world_batch_contract import _candidates, _target, _valid_result
from tests.test_setting_extractor import CHUNK_ID, DEFAULT_SCHEMA_HINTS


@pytest.fixture(autouse=True)
def forbid_network(monkeypatch):
    def reject_network(*args, **kwargs):
        raise AssertionError("Synthetic contract tests must not make network requests.")
    monkeypatch.setattr(socket.socket, "connect", reject_network)


def _reference(domain="characters"):
    return WorkerAnalysisReference(
        domain=domain, subject_name="에르웬", source_episode_no=1,
        reason="CHARACTER_MATCH_UNRESOLVED", setting_name="정신", value="35",
        evidence_spans=[{"quote": "에르웬인지 다른 인물인지 분명하지 않았다.", "startOffset": 2}],
    )


def _context():
    return _input_context().model_copy(update={
        "unresolved_references": [_reference(), _reference("worldSettings")],
    })


def _discovery(name="에르웬 미샤"):
    return ExtractedSettingCandidate(
        source_chunk_id=CHUNK_ID, candidate_kind="CHARACTER_DISCOVERY", entity_name=name,
        evidence_spans=[{"quote": f"에르웬이 자신의 이름을 {name}라고 말했다."}],
    )


def test_unresolved_reference_contract_defaults_and_rejects_internal_metadata():
    assert _input_context().unresolved_references == []
    reference = _reference().model_dump(by_alias=True)
    with pytest.raises(ValidationError):
        WorkerAnalysisReference.model_validate({**reference, "candidateId": str(uuid4())})
    with pytest.raises(ValidationError):
        WorkerAnalysisReference.model_validate({**reference, "domain": "HUMAN_REJECTION_POLICY"})
    with pytest.raises(ValidationError):
        AnalysisStateProvenance(confirmation_status="CONFIRMED", review_source="UNVERIFIED")
    payload = _ordered_payload()
    payload["analysis_context"] = _context()
    assert len(WorkerAnalysisJobPayload.model_validate(payload).analysis_context.unresolved_references) == 2
    payload["episode"]["episode_no"] = 1
    with pytest.raises(ValidationError, match="must precede"):
        WorkerAnalysisJobPayload.model_validate(payload)


def test_extraction_reads_aliases_review_source_and_unresolved_as_separate_context():
    character = WorkerAnalysisKnownCharacterPayload(
        character_id=uuid4(), name="에르웬", aliases=["에르웬 미샤"],
        identity_evidence=[{"quote": "에르웬이 이름을 밝혔다."}],
        provenance=AnalysisStateProvenance(confirmation_status="CONFIRMED", review_source="AUTOMATIC"),
    )
    context = OrderedExtractionContext(_context(), (character,), (_temporary_character(),))
    client = RecordingClient({"candidates": []})
    asyncio.run(CharacterSettingExtractor(llm_client=client, model="test-model").extract_from_chunk(
        source_chunk_id=CHUNK_ID, chunk_text="에르웬이 걸었다.", episode_no=2,
        schema_hints=DEFAULT_SCHEMA_HINTS, ordered_context=context,
    ))
    prompt = client.requests[0]["user_prompt"]
    assert '"aliases": ["에르웬 미샤"]' in prompt
    assert '"review_source": "AUTOMATIC"' in prompt
    assert '"confirmation_status": "UNRESOLVED"' in prompt
    assert '"applied_to_current_state": false' in prompt
    assert str(character.character_id) not in prompt
    assert str(context.analysis_context.run_id) not in prompt
    target = initial_ordered_subjects(context)[0]
    assert target.aliases == ("에르웬 미샤",)
    assert target.evidence == ("에르웬이 이름을 밝혔다.",)
    assert target.review_source == "AUTOMATIC"


def test_matched_discovery_is_saved_and_enriches_next_chunk_without_duplicate_person():
    character_id = uuid4()
    subjects = [OrderedSubjectTarget(name="에르웬", actual_character_id=character_id)]
    client = RecordingClient({"resolutions": [{"candidate_ref": "C1", "target_ref": "K1"}]})
    resolved, updates = asyncio.run(resolve_ordered_character_subjects(
        client=client, model="test-model", max_attempts=1, max_output_tokens=2000,
        context=SubjectResolutionChunkContext(None, "에르웬이 이름을 밝혔다.", None),
        candidates=[_discovery()], subjects=subjects, episode_no=2,
        unresolved_references=tuple(_context().unresolved_references),
    ))
    assert len(resolved) == 1
    assert resolved[0].binding.actual_character_id == character_id
    assert resolved[0].candidate.entity_name == "에르웬 미샤"
    assert resolved[0].binding.provisional_subject_key is None
    merge_ordered_subjects(subjects, updates)
    merge_ordered_subjects(subjects, updates)
    assert len(subjects) == 1
    assert subjects[0].aliases == ("에르웬 미샤",)
    assert len(subjects[0].evidence) == 1
    following = RecordingClient({"resolutions": [{"candidate_ref": "C1", "target_ref": "K1"}]})
    next_resolved, _ = asyncio.run(resolve_ordered_character_subjects(
        client=following, model="test-model", max_attempts=1, max_output_tokens=2000,
        context=SubjectResolutionChunkContext(None, "에르웬 미샤가 돌아왔다.", None),
        candidates=[_discovery()], subjects=subjects, episode_no=2,
    ))
    assert next_resolved[0].binding.actual_character_id == character_id
    assert client.requests == following.requests == []


def test_same_chunk_alias_discoveries_share_anchor_and_both_keep_source_evidence():
    client = RecordingClient({"resolutions": [
        {"candidate_ref": "C1", "target_ref": "D1"},
        {"candidate_ref": "C2", "target_ref": "D1"},
    ]})
    resolved, updates = asyncio.run(resolve_ordered_character_subjects(
        client=client, model="test-model", max_attempts=1, max_output_tokens=2000,
        context=SubjectResolutionChunkContext(None, "에르웬은 에르웬 미샤다.", None),
        candidates=[_discovery("에르웬"), _discovery()], subjects=[], episode_no=2,
    ))
    assert len(resolved) == 2 and len(updates) == 1
    assert resolved[0].binding.provisional_subject_key == resolved[1].binding.provisional_subject_key
    assert updates[0].aliases == ("에르웬 미샤",)
    assert len(updates[0].evidence) == 2


def test_ambiguous_discovery_never_adds_alias_to_known_character():
    subject = OrderedSubjectTarget(name="에르웬", actual_character_id=uuid4())
    client = RecordingClient({"resolutions": [{"candidate_ref": "C1", "target_ref": None}]})
    resolved, updates = asyncio.run(resolve_ordered_character_subjects(
        client=client, model="test-model", max_attempts=1, max_output_tokens=2000,
        context=SubjectResolutionChunkContext(None, "또 다른 에르웬 미샤였다.", None),
        candidates=[_discovery()], subjects=[
            subject, OrderedSubjectTarget(name="에르웬 미샤", actual_character_id=uuid4()),
        ], episode_no=2,
    ))
    assert updates == []
    assert resolved[0].binding.actual_character_id is None
    assert resolved[0].binding.match_status == "AMBIGUOUS"


def test_character_batch_pipeline_keeps_unresolved_out_of_snapshots_and_counts_input_limit():
    response = {"decisions": [_decision_payload(
        "C1", operation="REMOVE", resolved_key="status.부상", removed_refs=["P1"], value=None,
    )]}
    client = RecordingClient(response)
    comparator = CharacterFactComparator(llm_client=client, model="test-model")
    snapshot = _batch_snapshot("P1", "status.부상", "다친 다리").model_copy(update={
        "provenance": AnalysisStateProvenance(confirmation_status="CONFIRMED", review_source="AUTOMATIC"),
    })
    args = dict(
        matched_character_name="에르웬", canonical_fact_type="STATUS",
        candidates=[_candidate("C1", "Q1", "다리가 나아 일어섰다")],
        snapshot_entries=[snapshot], ordered_context=True,
        unresolved_references=tuple(_context().unresolved_references),
    )
    result = asyncio.run(execute_character_fact_comparison_batch(comparator, **args))
    assert not result.failures and len(result.decisions) == 1
    prompt = json.loads(client.requests[0]["user_prompt"])
    assert len(prompt["snapshot_entries"]) == 1
    assert prompt["snapshot_entries"][0]["review_source"] == "AUTOMATIC"
    assert len(prompt["unresolved_references"]) == 2
    comparator.batch_max_input_tokens = _estimate_prompt_tokens(
        client.requests[0]["system_prompt"], client.requests[0]["user_prompt"], comparator.model,
    ) + 1
    assert comparator.batch_fits(**args)
    oversized = _reference().model_copy(update={"value": "synthetic unresolved claim " * 20000})
    assert not comparator.batch_fits(**{**args, "unresolved_references": (oversized,)})
    with pytest.raises(ComparisonValidationError, match="explicit ordered"):
        asyncio.run(comparator.compare_batch(**{**args, "ordered_context": False}))


def test_world_subject_resolver_considers_unresolved_even_without_registered_subjects():
    client = RecordingClient({"selected_subject_refs": [], "ambiguous": True})
    candidate = WorkerWorldSettingSubjectResolutionCandidate(
        candidate_id=uuid4(), source_episode_id=uuid4(), category="RACE",
        subject_name="합성 종족", evidence_spans=[{"quote": "새 합성 종족이 나타났다."}],
    )
    resolver = WorldSettingSubjectResolver(llm_client=client, model="test-model")
    selected, ambiguous = asyncio.run(resolver.select_ordered_subjects(
        candidate, [], unresolved_references=tuple(_context().unresolved_references),
    ))
    assert selected == [] and ambiguous is True
    prompt = json.loads(client.requests[0]["user_prompt"])
    assert prompt["subjects"] == []
    assert len(prompt["unresolved_references"]) == 2


def test_world_batch_unresolved_claims_are_not_selectable_target_properties():
    client = RecordingClient(_valid_result())
    comparator = WorldSettingComparator(llm_client=client, model="test-model")
    target = _target(provisional=False)
    target.properties[0].provenance = target.properties[0].provenance.model_copy(update={"review_source": "HUMAN"})
    result, _ = asyncio.run(comparator.compare_batch(
        "RACE", _candidates(), [target], ordered_context=True,
        unresolved_references=tuple(_context().unresolved_references),
    ))
    assert len(result.decisions) == 7
    prompt = json.loads(client.requests[0]["user_prompt"])
    assert len(prompt["targets"]) == 1 and len(prompt["targets"][0]["properties"]) == 1
    assert prompt["targets"][0]["properties"][0]["review_source"] == "HUMAN"
    assert prompt["targets"][0]["properties"][0]["ref"] == "T1.P1"
    assert len(prompt["unresolved_references"]) == 2


def test_world_extraction_references_do_not_change_legacy_requests():
    client = RecordingClient({"candidates": []}, {"candidates": []}, {"candidates": []})
    extractor = WorldSettingExtractor(llm_client=client, model="test-model")
    asyncio.run(extractor.extract_from_chunk("새 원고", episode_no=2))
    asyncio.run(extractor.extract_from_chunk("새 원고", episode_no=2, analysis_context=_context()))
    asyncio.run(extractor.extract_from_chunk("새 원고", episode_no=2))
    assert client.requests[0] == client.requests[2]
    assert "UNRESOLVED" not in client.requests[0]["user_prompt"]
    assert '"applied_to_current_state": false' in client.requests[1]["user_prompt"]
    assert "unresolved_references" in client.requests[1]["system_prompt"]


def test_world_pipeline_forwards_frozen_references_to_subject_resolution_and_comparison():
    from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
    from tests.test_world_setting_batch_pipeline import (
        FakeBatchComparator, PreparingBatchSpringApi, _batch, _merged_decision,
    )

    context = _context()
    batch = _batch().model_copy(update={"analysis_context": context})

    class Spring(PreparingBatchSpringApi):
        async def get_pending_world_setting_subject_resolutions(self, *args):
            result = await super().get_pending_world_setting_subject_resolutions(*args)
            return result.model_copy(update={"analysis_context": context})

        async def get_world_setting_comparison_batch_context(self, *args, provisional_subject_keys=None):
            result = await super().get_world_setting_comparison_batch_context(*args)
            return result.model_copy(update={"analysis_context": context, "context_token": "c" * 64})

    class Comparator(FakeBatchComparator):
        async def compare_batch(self, category, candidates, targets, **kwargs):
            assert kwargs["ordered_context"] is True
            assert kwargs["unresolved_references"] == tuple(context.unresolved_references)
            return await super().compare_batch(category, candidates, targets)

    client = RecordingClient(*[
        {"selected_subject_refs": ["S1"], "ambiguous": False} for _ in batch.candidates
    ])
    spring = Spring([batch])
    pipeline = WorldSettingComparisonPipeline(
        spring, WorldSettingSubjectResolver(llm_client=client, model="test-model"),
        Comparator(_merged_decision()),
    )

    async def scenario():
        await pipeline._prepare_subject_resolutions(uuid4(), uuid4(), context)
        await pipeline._compare_batch_with_fresh_context(uuid4(), uuid4(), batch, 1, [])

    asyncio.run(scenario())
    assert len(client.requests) == len(batch.candidates)
    assert all(len(json.loads(call["user_prompt"])["unresolved_references"]) == 2 for call in client.requests)
    assert len(spring.completions) == 1
    assert spring.completions[0].context_token == "c" * 64


def test_character_pipeline_reads_references_from_the_frozen_batch_context():
    from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
    from tests.test_character_fact_batch import FakeBatchSpring, _batch, _context as character_context

    context = _context()
    batch = _batch().model_copy(update={
        "analysis_context": context,
        "candidates": [_candidate("C1", "Q1", "부상에서 완전히 회복되었다")],
    })
    snapshot = _batch_snapshot("P1", "status.부상", "다친 다리").model_copy(update={
        "provenance": AnalysisStateProvenance(confirmation_status="CONFIRMED", review_source="AUTOMATIC"),
    })
    comparison_context = character_context(batch).model_copy(update={
        "analysis_context": context, "snapshot_entries": [snapshot],
    })
    spring = FakeBatchSpring(batch, comparison_context)
    client = RecordingClient({"decisions": [_decision_payload(
        "C1", operation="REMOVE", resolved_key="status.부상", removed_refs=["P1"], value=None,
    )]})
    pipeline = CharacterFactComparisonPipeline(spring, CharacterFactComparator(llm_client=client, model="test-model"))
    asyncio.run(pipeline._compare_batch_with_fresh_context(uuid4(), uuid4(), batch))
    assert len(spring.completions) == 1
    assert len(json.loads(client.requests[0]["user_prompt"])["unresolved_references"]) == 2

    # Even the same input hash cannot hide changed transport reference contents.
    spring.context = comparison_context.model_copy(update={"analysis_context": _input_context().model_copy(update={
        **context.model_dump(), "unresolved_references": [_reference().model_copy(update={"value": "changed"})],
    })})
    with pytest.raises(ComparisonValidationError, match="context changed"):
        asyncio.run(pipeline._compare_batch_with_fresh_context(uuid4(), uuid4(), batch))
    assert len(client.requests) == 1


def test_analysis_worker_passes_frozen_references_to_both_extraction_stages():
    from app.analysis.character_subject_resolver import CharacterSubjectResolver
    from app.worker.analysis_job_worker import AnalysisJobWorker
    from tests.test_analysis_job_worker import FakeSpringWorkerClient, _chunk

    payload_data = _ordered_payload()
    payload_data["analysis_context"] = _context()
    payload = WorkerAnalysisJobPayload.model_validate(payload_data)
    client = RecordingClient(
        {"candidates": [_discovery().model_dump(mode="json", exclude={"source_chunk_id"})]},
        {"candidates": []},
    )

    class Store:
        def replace_candidates_for_analysis_job(self, **kwargs):
            assert kwargs["ordered_write_context"].context == payload.analysis_context
            return kwargs["save_items"]

    worker = AnalysisJobWorker(
        spring_client=FakeSpringWorkerClient(payload), setting_candidate_service=Store(),
        setting_extractor=CharacterSettingExtractor(llm_client=client, model="test-model"),
        subject_resolver=CharacterSubjectResolver(llm_client=client, model="test-model"),
        world_setting_extractor=WorldSettingExtractor(llm_client=client, model="test-model"),
    )

    async def scenario():
        try:
            chunks = [_chunk(0, "에르웬이 자신의 이름을 에르웬 미샤라고 말했다.")]
            await worker._run_character_stage(payload, chunks, None, [], DEFAULT_SCHEMA_HINTS)
            await worker._run_world_extraction_stage(payload, chunks, None)
        finally:
            await worker.aclose()

    asyncio.run(scenario())
    assert len(client.requests) == 2
    for request in client.requests:
        assert "unresolved_references" in request["user_prompt"]
        assert '"applied_to_current_state": false' in request["user_prompt"]
        assert str(payload.analysis_context.run_id) not in request["user_prompt"]
