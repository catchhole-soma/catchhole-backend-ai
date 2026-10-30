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
from app.core.config import Settings
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

    class Encoding:
        def encode(self, text, **kwargs):
            return list(text.encode("utf-8"))

    # These tests check context propagation and relative input growth, not the
    # provider vocabulary. A cold tiktoken cache would otherwise fetch that
    # vocabulary before the synthetic request reaches our recording client.
    monkeypatch.setattr("app.analysis.ordered_context.tiktoken.get_encoding", lambda name: Encoding())
    monkeypatch.setattr(socket.socket, "connect", reject_network)
    # The offline tokenizer counts bytes; this budget isolates response rules
    # from the production token cap, covered by the real-tokenizer packing tests.
    monkeypatch.setattr("app.analysis.world_setting_comparator.get_settings", lambda: Settings(
        _env_file=None, llm_world_setting_batch_comparison_max_input_tokens=64000,
    ))


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
        identity_evidence=[{"quote": "에르웬이 이름을 밝혔다.", "startOffset": 0,
                            "endOffset": len("에르웬이 이름을 밝혔다.")}],
        provenance=AnalysisStateProvenance(confirmation_status="CONFIRMED", review_source="AUTOMATIC"),
    )
    context = OrderedExtractionContext(_context(), (character,), (_temporary_character(),))
    client = RecordingClient({"candidates": []})
    asyncio.run(CharacterSettingExtractor(llm_client=client, model="test-model").extract_from_chunk(
        source_chunk_id=CHUNK_ID, chunk_text="에르웬이 걸었다.", episode_no=2,
        schema_hints=DEFAULT_SCHEMA_HINTS, ordered_context=context,
    ))
    prompt = client.requests[0]["user_prompt"]
    assert 'pending_character_hints' in prompt
    assert '"names": ["에르웬"]' in prompt
    assert 'identity_evidence' not in prompt and 'review_source' not in prompt
    assert 'CHARACTER_MATCH_UNRESOLVED' not in prompt
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
    assert len(prompt["unresolved_references"]) == 1
    comparator.batch_max_input_tokens = _estimate_prompt_tokens(
        client.requests[0]["system_prompt"], client.requests[0]["user_prompt"], comparator.model,
    ) + 1
    assert asyncio.run(comparator.batch_fits(**args))
    oversized = _reference().model_copy(update={"value": "synthetic unresolved claim " * 20000})
    assert not asyncio.run(comparator.batch_fits(**{**args, "unresolved_references": (oversized,)}))
    with pytest.raises(ComparisonValidationError, match="explicit ordered"):
        asyncio.run(comparator.compare_batch(**{**args, "ordered_context": False}))


def test_world_subject_resolver_considers_unresolved_even_without_registered_subjects():
    client = RecordingClient({"selected_subject_refs": [], "ambiguous": True})
    candidate = WorkerWorldSettingSubjectResolutionCandidate(
        candidate_id=uuid4(), source_episode_id=uuid4(), category="RACE",
        subject_name="합성 종족", evidence_spans=[{"quote": "새 합성 종족이 나타났다."}],
    )
    resolver = WorldSettingSubjectResolver(llm_client=client, model="test-model")
    related = _reference("worldSettings").model_copy(update={"subject_name": candidate.subject_name})
    selected, ambiguous = asyncio.run(resolver.select_ordered_subjects(
        candidate, [], unresolved_references=(_reference(), related),
    ))
    assert selected == [] and ambiguous is True
    prompt = json.loads(client.requests[0]["user_prompt"])
    assert prompt["subjects"] == []
    assert len(prompt["unresolved_references"]) == 1


def test_world_batch_unresolved_claims_are_not_selectable_target_properties():
    client = RecordingClient(_valid_result())
    comparator = WorldSettingComparator(llm_client=client, model="test-model")
    target = _target(provisional=False)
    target.properties[0].provenance = target.properties[0].provenance.model_copy(update={"review_source": "HUMAN"})
    result, _ = asyncio.run(comparator.compare_batch(
        "RACE", _candidates(), [target], ordered_context=True,
        unresolved_references=(_reference(), _reference("worldSettings").model_copy(update={"subject_name": target.subject_name})),
    ))
    assert len(result.decisions) == 7
    prompt = json.loads(client.requests[0]["user_prompt"])
    assert len(prompt["targets"]) == 1 and len(prompt["targets"][0]["properties"]) == 1
    assert prompt["targets"][0]["properties"][0]["review_source"] == "HUMAN"
    assert prompt["targets"][0]["properties"][0]["ref"] == "T1.P1"
    assert [ref["domain"] for ref in prompt["unresolved_references"]] == ["worldSettings"]
    assert prompt["unresolved_references"][0]["applied_to_current_state"] is False


def test_world_extraction_references_do_not_change_legacy_requests():
    client = RecordingClient({"candidates": []}, {"candidates": []}, {"candidates": []})
    extractor = WorldSettingExtractor(llm_client=client, model="test-model")
    asyncio.run(extractor.extract_from_chunk("새 원고", episode_no=2))
    asyncio.run(extractor.extract_from_chunk("새 원고", episode_no=2, analysis_context=_context()))
    asyncio.run(extractor.extract_from_chunk("새 원고", episode_no=2))
    assert client.requests[0] == client.requests[2]
    assert "UNRESOLVED" not in client.requests[0]["user_prompt"]
    assert client.requests[0]["user_prompt"] == client.requests[1]["user_prompt"]
    assert "unresolved_references" not in client.requests[1]["system_prompt"]


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

    client = RecordingClient({"decisions": [
        {"candidate_ref": candidate.candidate_ref, "selected_subject_refs": ["S1"],
         "ambiguous": False, "identity_proofs": []} for candidate in batch.candidates
    ]})
    spring = Spring([batch])
    pipeline = WorldSettingComparisonPipeline(
        spring, WorldSettingSubjectResolver(llm_client=client, model="test-model"),
        Comparator(_merged_decision()),
    )

    async def scenario():
        await pipeline._prepare_subject_resolutions(uuid4(), uuid4(), context)
        await pipeline._compare_batch_with_fresh_context(uuid4(), uuid4(), batch, 1, [])

    asyncio.run(scenario())
    assert len(client.requests) == 1
    assert all(json.loads(call["user_prompt"])["unresolved_references"] == [] for call in client.requests)
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
    # The batch's different character does not inherit the work-level pending name.
    assert json.loads(client.requests[0]["user_prompt"])["unresolved_references"] == []

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
    assert "pending_character_hints" in client.requests[0]["user_prompt"]
    assert "unresolved_references" not in client.requests[1]["user_prompt"]
    for request in client.requests:
        assert "semantic_current_state" not in request["user_prompt"]
        assert str(payload.analysis_context.run_id) not in request["user_prompt"]


def test_gh287_episode_first_stage_is_lean_and_world_has_no_pending_input():
    context = OrderedExtractionContext(_context(), (), (_temporary_character(),))
    client = RecordingClient({'candidates': []})
    asyncio.run(CharacterSettingExtractor(llm_client=client, model='test-model').extract_from_chunk(
        source_chunk_id=CHUNK_ID, chunk_text='이번 원문.', episode_no=2,
        schema_hints=DEFAULT_SCHEMA_HINTS, ordered_context=context))
    request = client.requests[0]
    assert 'pending_character_hints' in request['user_prompt']
    assert '이전 원문의 근거' not in request['user_prompt']
    assert 'CHARACTER_MATCH_UNRESOLVED' not in request['user_prompt']
    assert 'ordered_character_state' not in request['user_prompt']
    assert 'semantic_current_state' not in request['user_prompt']
    assert '다시 발견할 필요는 없' not in request['user_prompt']
    world = RecordingClient({'candidates': []})
    asyncio.run(WorldSettingExtractor(llm_client=world, model='test-model').extract_from_chunk(
        chunk_text='이번 원문.', episode_no=2, analysis_context=_context()))
    assert world.requests[0]['user_prompt'] == 'metadata:\n{"episode_no": 2, "episode_title": null}\n\nchunk_text:\n이번 원문.'
    assert 'unresolved_references는' not in world.requests[0]['system_prompt']


def test_gh287_comparison_selects_related_domain_and_known_identity_before_token_count():
    subject_id = uuid4()
    related = _reference().model_copy(update={'setting_name': 'status.부상', 'value': 'related injury'})
    wrong = WorkerAnalysisReference.model_validate({**related.model_dump(by_alias=True),
        'value': 'wrong same-name identity', 'selection': {'actualSubjectIds': [str(uuid4())]}})
    linked = WorkerAnalysisReference.model_validate({**related.model_dump(by_alias=True),
        'selection': {'actualSubjectIds': [str(subject_id)]}})
    unrelated = related.model_copy(update={'subject_name': '무관한 사람', 'value': 'unrelated' * 5000})
    refs = (wrong, linked, unrelated, _reference('worldSettings'))
    client = RecordingClient({'decisions': [_decision_payload('C1', operation='ADD', resolved_key='status.부상', value='회복')]})
    comparator = CharacterFactComparator(llm_client=client, model='test-model')
    args = dict(matched_character_name='에르웬', canonical_fact_type='STATUS',
        candidates=[_candidate('C1', 'Q1', '회복')], snapshot_entries=[], ordered_context=True,
        unresolved_references=refs, advisory_current_context={'characterId': str(subject_id)})
    asyncio.run(comparator.compare_batch(**args))
    prompt = json.loads(client.requests[0]['user_prompt'])
    assert [item['value'] for item in prompt['unresolved_references']] == ['related injury']
    assert str(subject_id) not in client.requests[0]['user_prompt']
    assert asyncio.run(comparator.batch_fits(**args))
    assert len(refs) == 4


def test_gh287_provisional_link_survives_changed_display_name_through_shared_builder():
    from app.analysis.pending_reference_projection import PendingSubjectScope
    key = 'provisional-character:synthetic'
    item = WorkerAnalysisReference.model_validate({**_reference().model_dump(by_alias=True),
        'subjectName': '이전 표기', 'selection': {'provisionalSubjectKeys': [key]}})
    client = RecordingClient({'decisions': [_decision_payload('C1', operation='ADD', resolved_key='status.부상', value='회복')]})
    comparator = CharacterFactComparator(llm_client=client, model='test-model')
    args = dict(matched_character_name='새 표기', canonical_fact_type='STATUS',
        candidates=[_candidate('C1', 'Q1', '회복')], snapshot_entries=[], ordered_context=True,
        unresolved_references=(item,), pending_subject_scope=PendingSubjectScope(
            'characters', provisional_subject_keys=(key,), names=('새 표기',)))
    asyncio.run(execute_character_fact_comparison_batch(comparator, **args))
    assert json.loads(client.requests[0]['user_prompt'])['unresolved_references'][0]['subject_name'] == '이전 표기'
    assert key not in client.requests[0]['user_prompt']


def test_gh287_episode_keeps_static_temporal_rules_and_provisional_status_provenance():
    from app.analysis.semantic_current_context import CURRENT_STATUS_FORECAST_INSTRUCTIONS
    from app.schemas.worker import WorkerAnalysisActiveCharacterStatusPayload
    provisional = _temporary_character().model_copy(update={'active_statuses': [
        WorkerAnalysisActiveCharacterStatusPayload(fact_key='status.부상', fact_value='골절',
            provenance=AnalysisStateProvenance(confirmation_status='PROVISIONAL', source_episode_no=1))]})
    client = RecordingClient({'candidates': []})
    asyncio.run(CharacterSettingExtractor(llm_client=client, model='test-model').extract_from_chunk(
        source_chunk_id=CHUNK_ID, chunk_text='이번 원문.', episode_no=2,
        schema_hints=DEFAULT_SCHEMA_HINTS, ordered_context=OrderedExtractionContext(_context(), (), (provisional,))))
    request = client.requests[0]
    assert CURRENT_STATUS_FORECAST_INSTRUCTIONS in request['system_prompt']
    assert 'PROVISIONAL' in request['user_prompt']
    assert 'status.부상' in request['user_prompt']
    assert 'identity_evidence' not in request['user_prompt']


def test_gh287_real_character_batch_uses_frozen_aliases_without_losing_id_mismatch_guard():
    from app.analysis.character_fact_comparison_pipeline import CharacterFactComparisonPipeline
    from tests.test_character_fact_batch import FakeBatchSpring, _batch, _context as character_context
    subject_id = uuid4()
    related = _reference().model_copy(update={'subject_name': '옛별명'})
    wrong = WorkerAnalysisReference.model_validate({**related.model_dump(by_alias=True),
        'selection': {'actualSubjectIds': [str(uuid4())]}})
    frozen = _input_context().model_copy(update={'unresolved_references': [related, wrong]})
    batch = _batch().model_copy(update={'analysis_context': frozen, 'matched_character_name': '정식이름',
        'candidates': [_candidate('C1', 'Q1', '회복')]})
    context = character_context(batch).model_copy(update={'analysis_context': frozen,
        'matched_character_name': '정식이름', 'snapshot_entries': [],
        'advisory_current_context': {'characterId': str(subject_id), 'selectionNames': ['정식이름', '옛별명']}})
    client = RecordingClient({'decisions': [_decision_payload('C1', operation='ADD', resolved_key='status.부상', value='회복')]})
    pipeline = CharacterFactComparisonPipeline(FakeBatchSpring(batch, context),
        CharacterFactComparator(llm_client=client, model='test-model'))
    asyncio.run(pipeline._compare_batch_with_fresh_context(uuid4(), uuid4(), batch))
    prompt = json.loads(client.requests[0]['user_prompt'])
    assert [item['subject_name'] for item in prompt['unresolved_references']] == ['옛별명']
    assert 'selectionNames' not in client.requests[0]['user_prompt']


def test_gh287_world_resolved_target_keeps_candidate_spelling_with_same_id_guard():
    target = _target(provisional=False).model_copy(update={'subject_name': '정식이름'})
    candidates = [candidate.model_copy(update={'subject_name': '옛별명'}) for candidate in _candidates()]
    related = _reference('worldSettings').model_copy(update={'subject_name': '옛별명'})
    wrong = WorkerAnalysisReference.model_validate({**related.model_dump(by_alias=True),
        'selection': {'actualSubjectIds': [str(uuid4())]}})
    client = RecordingClient(_valid_result())
    asyncio.run(WorldSettingComparator(llm_client=client, model='test-model').compare_batch(
        'RACE', candidates, [target], ordered_context=True, unresolved_references=(related, wrong)))
    prompt = json.loads(client.requests[0]['user_prompt'])
    assert [item['subject_name'] for item in prompt['unresolved_references']] == ['옛별명']


def test_gh287_large_unrelated_context_is_filtered_before_exact_count_and_schema_retry():
    from app.analysis.ordered_context import ensure_ordered_prompt_fits
    from app.llm.responses import LlmTextResponse
    related = _reference().model_copy(update={'value': '다리 부상'})
    refs = (related, *(_reference().model_copy(update={
        'subject_name': f'무관인물{index}', 'value': '무관한 합성 설정 ' * 80}) for index in range(143)))

    class CountedProvider:
        def __init__(self):
            self.requests, self.counts = [], []

        async def count_input_tokens(self, **kwargs):
            self.counts.append(kwargs)
            return ensure_ordered_prompt_fits(kwargs['system_prompt'], kwargs['user_prompt'],
                max_tokens=1000000, response_schema=kwargs.get('response_schema'))

        async def create_text_response(self, **kwargs):
            self.requests.append(kwargs)
            return LlmTextResponse(text='invalid json' if len(self.requests) == 1 else json.dumps({
                'decisions': [_decision_payload('C1', operation='ADD', resolved_key='status.부상', value='부상')]}))

    provider = CountedProvider()
    comparator = CharacterFactComparator(llm_client=provider, model='gpt-6.1-sol', max_attempts=2)
    args = dict(matched_character_name='에르웬', canonical_fact_type='STATUS',
        candidates=[_candidate('C1', 'Q1', '부상')], snapshot_entries=[], ordered_context=True,
        unresolved_references=refs)
    assert asyncio.run(comparator.batch_fits(**args))
    asyncio.run(comparator.compare_batch(**args))
    assert len(provider.requests) == 2
    for request in provider.requests:
        payload = json.loads(request['user_prompt'])
        assert [item['value'] for item in payload['unresolved_references']] == ['다리 부상']
        assert any(count['user_prompt'] == request['user_prompt'] and count['system_prompt'] == request['system_prompt']
            and count.get('response_schema') == request.get('response_schema') for count in provider.counts)
    request = provider.requests[0]
    full = json.loads(request['user_prompt'])
    full['unresolved_references'] = [item.prompt_data() for item in refs]
    before = ensure_ordered_prompt_fits(request['system_prompt'], json.dumps(full, ensure_ascii=False),
        max_tokens=1000000, response_schema=request.get('response_schema'))
    after = ensure_ordered_prompt_fits(request['system_prompt'], request['user_prompt'],
        max_tokens=1000000, response_schema=request.get('response_schema'))
    assert before > after * 2
    assert len(refs) == 144 and refs[-1].value.startswith('무관한 합성 설정')


def test_gh287_forced_character_split_keeps_frozen_scope_in_counts_and_child_requests():
    from app.analysis.pending_reference_projection import PendingSubjectScope
    from app.llm.exceptions import LlmOutputTruncatedError
    from app.llm.responses import LlmTextResponse
    key = f'provisional-character:{uuid4()}'
    related = WorkerAnalysisReference.model_validate({**_reference().model_dump(by_alias=True),
        'subjectName': '옛 표기', 'selection': {'provisionalSubjectKeys': [key]}})
    refs = (related, _reference().model_copy(update={'subject_name': '무관인물'}), _reference('worldSettings'))
    sources = [_candidate('C1', 'Q1', '부상'), _candidate('C2', 'Q2', '두통').model_copy(update={
        'raw_fact_key': 'status.두통', 'initial_canonical_fact_key': 'status.두통'})]

    class SplitProvider:
        def __init__(self):
            self.requests, self.counts = [], []

        async def count_input_tokens(self, **kwargs):
            self.counts.append(kwargs)
            return 1000

        async def create_text_response(self, **kwargs):
            self.requests.append(kwargs)
            candidates = json.loads(kwargs['user_prompt'])['candidates']
            if len(candidates) == 2:
                raise LlmOutputTruncatedError('synthetic output cap', incomplete_reason='max_output_tokens', max_output_tokens=16000)
            row = candidates[0]
            return LlmTextResponse(text=json.dumps({'decisions': [_decision_payload(
                row['candidate_ref'], operation='ADD', resolved_key=row['initial_canonical_fact_key'], value=row['attribute_value'])]}))

    provider = SplitProvider()
    result = asyncio.run(execute_character_fact_comparison_batch(
        CharacterFactComparator(llm_client=provider, model='gpt-6.1-sol', max_attempts=1),
        matched_character_name='새 표기', canonical_fact_type='STATUS', candidates=sources,
        snapshot_entries=[], ordered_context=True, unresolved_references=refs,
        pending_subject_scope=PendingSubjectScope('characters', provisional_subject_keys=(key,), names=('새 표기',))))
    assert len(provider.requests) == 3 and len(result.decisions) == 2 and not result.failures
    for request in provider.requests:
        payload = json.loads(request['user_prompt'])
        assert [item['subject_name'] for item in payload['unresolved_references']] == ['옛 표기']
        assert any(count['user_prompt'] == request['user_prompt'] and count['system_prompt'] == request['system_prompt']
            and count.get('response_schema') == request.get('response_schema') for count in provider.counts)
        assert key not in request['user_prompt']
    assert len(json.loads(provider.requests[-1]['user_prompt'])['snapshot_entries']) == 1
    assert len(refs) == 3 and related.selection.provisional_subject_keys == (key,)
