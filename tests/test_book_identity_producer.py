"""BOOK identity proof contracts, exercised without provider, HTTP, or DB calls."""

import asyncio
import copy
import json
from dataclasses import replace
from hashlib import sha256
from uuid import uuid4

import pytest

from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.exceptions import ComparisonValidationError
from app.analysis.ordered_character_subjects import resolve_ordered_character_subjects
from tests.test_ordered_subject_rules import RecordingClient, _discovery, _known, _setting
from tests.test_ordered_subject_rules import no_network as no_network
from tests.test_setting_book_reference_time import book_source

NAME = "백솔"
FIRST = "[인물: 백솔 / 기록관]\r\n성별: 여자\r\n나이: 28"
SECOND = "[인물: 백솔 / 선원]\r\n성별: 여자\r\n나이: 34"
DISTINCT = "위의 기록관 백솔과 선원 백솔은 이름만 같은 서로 다른 인물이다."
TEXT = "📚 인물 자료\r\n" + FIRST + "\r\n\r\n" + SECOND + "\r\n\r\n" + DISTINCT


def span(quote):
    start = TEXT.index(quote)
    return {"quote": quote, "start_offset": start, "end_offset": start + len(quote)}


def candidates():
    rows = [
        _discovery(NAME, quote=FIRST.split("\r\n")[0]),
        _discovery(NAME, quote=SECOND.split("\r\n")[0]),
        _setting(NAME, raw="기록관 백솔", quote="성별: 여자"),
        _setting(NAME, raw="선원 백솔", quote="성별: 여자"),
    ]
    for row in rows[2:]:
        row.attribute_name = "profile.gender"
        row.attribute_value = "여자"
        row.value_type = "STRING"
        row.value_json = {"value": "여자"}
    for row in rows[:2]:
        row.evidence_spans[0].start_offset = TEXT.index(row.evidence_spans[0].quote)
        row.evidence_spans[0].end_offset = row.evidence_spans[0].start_offset + len(
            row.evidence_spans[0].quote
        )
    return rows


def response(refs=("D1", "D2")):
    return {
        "resolutions": [
            {"candidate_ref": f"C{i}", "target_ref": refs[(i - 1) % 2]} for i in range(1, 5)
        ],
        "identity_groups": [
            {
                "group_ref": "G1",
                "decision": "DISTINCT_IDENTITIES",
                "normalized_name": NAME,
                "members": [
                    {"target_ref": ref, "profile_evidence": span(profile)}
                    for ref, profile in zip(refs, (FIRST, SECOND), strict=True)
                ],
                "distinct_evidence_spans": [span(DISTINCT)],
            }
        ],
    }


def resolve(rows, result, *, subjects=(), source=None, **kwargs):
    client = RecordingClient(result)
    source = source or book_source().model_copy(
        update={"content_hash": sha256(TEXT.encode()).hexdigest()}
    )
    resolved, updates = asyncio.run(
        resolve_ordered_character_subjects(
            client=client,
            model="offline-test-model",
            max_attempts=1,
            max_output_tokens=5000,
            context=SubjectResolutionChunkContext(None, TEXT, None),
            candidates=rows,
            subjects=list(subjects),
            episode_no=None,
            analysis_source=source,
            **kwargs,
        )
    )
    return resolved, updates, client


def test_book_typed_identity_groups_are_accepted_with_existing_binding_decisions():
    resolved, updates, provider = resolve(candidates(), response())
    first, second = [item.binding.provisional_subject_key for item in resolved[:2]]
    assert first != second and first and second
    assert [item.binding.provisional_subject_key for item in resolved[2:]] == [first, second]
    assert len(updates) == 2 and len(provider.requests) == 1
    assert "identity_groups" in provider.requests[0]["response_schema"].schema["properties"]


def test_book_same_name_actual_reconnect_uses_typed_resolver_without_new_discoveries():
    actual = [_known(NAME), _known(NAME)]
    rows = [
        _setting(NAME, raw="기록관 백솔", quote=FIRST),
        _setting(NAME, raw="선원 백솔", quote=SECOND),
    ]
    result = response(("K1", "K2"))
    result["resolutions"] = result["resolutions"][:2]
    resolved, _, provider = resolve(rows, result, subjects=actual)
    assert [item.binding.actual_character_id for item in resolved] == [
        item.actual_character_id for item in actual
    ]
    assert len(provider.requests) == 1
    prompt = provider.requests[0]["user_prompt"]
    assert all(str(item.actual_character_id) not in prompt for item in actual)


def identity_context(source=None, job=None):
    from uuid import uuid4

    from app.analysis.setting_book_identity import BookIdentityContext

    source = source or book_source().model_copy(
        update={"content_hash": sha256(TEXT.encode()).hexdigest()}
    )
    return BookIdentityContext(job or uuid4(), source, TEXT)


def trusted(rows=None, result=None, *, ctx=None, **kwargs):
    ctx = ctx or identity_context()
    return resolve(
        rows or candidates(),
        result or response(),
        source=ctx.source,
        book_identity_context=ctx,
        **kwargs,
    )


def test_frozen_group_translates_only_local_refs_and_repairs_repeated_child_inside_selected_parent():
    ctx = identity_context()
    resolved, _, provider = trusted(ctx=ctx)
    proofs = [item.binding.book_identity_proof.wire() for item in resolved]
    parent_refs = [item.binding.provisional_subject_key for item in resolved[:2]]
    for index, proof in enumerate(proofs):
        assert proof["parentTargetRef"] == parent_refs[index % 2]
        assert proof["sourceAnalysisJobId"] == str(ctx.analysis_job_id)
        assert proof["revisionId"] == str(ctx.source.revision_id)
        assert proof["referenceEpisodeNo"] is None
        assert proof["referenceTimeKind"] == "WORK_START"
        assert [member["targetRef"] for member in proof["members"]] == parent_refs
        assert proof["members"] == proofs[0]["members"]
        assert proof["distinctEvidenceSpans"] == proofs[0]["distinctEvidenceSpans"]
    positions = [row.candidate.evidence_spans[0].start_offset for row in resolved[2:]]
    assert positions == [TEXT.index("성별: 여자"), TEXT.index("성별: 여자", TEXT.index(SECOND))]
    prompt = provider.requests[0]["user_prompt"]
    for value in [
        ctx.analysis_job_id,
        ctx.source.setting_book_id,
        ctx.source.revision_id,
        ctx.source.content_hash,
        *(row.binding.candidate_id for row in resolved),
    ]:
        assert str(value) not in prompt
    assert "frozen_source" in json.loads(prompt)["context"]


@pytest.mark.parametrize(
    "bad",
    [
        "unknown_target",
        "null_member",
        "missing_quote",
        "missing_distinct",
        "bad_decision",
        "bad_group_ref",
        "duplicate_member",
        "overlap",
        "utf16_offset",
        "wrong_quote",
        "partial_group",
    ],
)
def test_invalid_typed_partition_cannot_produce_proof(bad):
    result = response()
    group = result["identity_groups"][0]
    if bad == "unknown_target":
        group["members"][0]["target_ref"] = "K999"
    elif bad == "null_member":
        group["members"][0]["target_ref"] = None
    elif bad == "missing_quote":
        group["members"][0]["profile_evidence"].pop("quote")
    elif bad == "missing_distinct":
        group["distinct_evidence_spans"] = []
    elif bad == "bad_decision":
        group["decision"] = "SAME_PERSON"
    elif bad == "bad_group_ref":
        group["group_ref"] = "G0"
    elif bad == "duplicate_member":
        group["members"][1] = copy.deepcopy(group["members"][0])
    elif bad == "overlap":
        group["members"][1]["profile_evidence"] = span(FIRST)
    elif bad == "utf16_offset":
        group["members"][0]["profile_evidence"]["start_offset"] += 1
        group["members"][0]["profile_evidence"]["end_offset"] += 1
    elif bad == "wrong_quote":
        group["members"][0]["profile_evidence"]["quote"] = "본문에 없는 프로필"
    elif bad == "partial_group":
        group["members"].pop()
    resolved, _, _ = trusted(result=result, continue_on_candidate_failure=True)
    assert all(row.binding.book_identity_proof is None for row in resolved)
    assert all(row.binding.match_status == "AMBIGUOUS" for row in resolved)


@pytest.mark.parametrize("bad", ["wrong_parent", "partial_offsets", "wrong_located_slice"])
def test_child_ownership_uses_its_own_exact_evidence(bad):
    rows = candidates()
    evidence = rows[2].evidence_spans[0]
    if bad == "wrong_parent":
        evidence.quote = "나이: 34"
        evidence.start_offset = TEXT.index(evidence.quote)
        evidence.end_offset = evidence.start_offset + len(evidence.quote)
    elif bad == "partial_offsets":
        evidence.start_offset = TEXT.index(evidence.quote)
    elif bad == "wrong_located_slice":
        evidence.start_offset, evidence.end_offset = 0, len(evidence.quote)
    result = response()
    resolved, _, _ = trusted(rows, result, continue_on_candidate_failure=True)
    assert resolved[2].binding.book_identity_proof is None
    assert resolved[2].binding.match_status == "AMBIGUOUS"
    assert resolved[2].binding.provisional_subject_key is None
    assert resolved[2].binding.actual_character_id is None
    assert resolved[2].binding.subject_failure_code is None
    assert all(row.binding.book_identity_proof for index, row in enumerate(resolved) if index != 2)


def test_null_binding_keeps_that_child_reviewable_without_borrowing_a_parent():
    result = response()
    result["resolutions"][2]["target_ref"] = None
    resolved, _, _ = trusted(result=result)
    assert resolved[2].binding.match_status == "AMBIGUOUS"
    assert resolved[2].binding.book_identity_proof is None
    assert resolved[2].candidate.evidence_spans[0].start_offset is None
    assert all(row.binding.book_identity_proof for index, row in enumerate(resolved) if index != 2)


def test_same_person_or_unproven_partition_keeps_existing_fallback_without_proof():
    result = response()
    result["identity_groups"] = []
    result["resolutions"] = [{"candidate_ref": f"C{i}", "target_ref": "D1"} for i in range(1, 5)]
    resolved, _, _ = trusted(result=result)
    assert len({row.binding.provisional_subject_key for row in resolved}) == 1
    assert all(row.binding.book_identity_proof is None for row in resolved)


def test_full_actual_reconnect_proof_keeps_supplied_real_ids_and_no_new_identities():
    actual = [_known(NAME), _known(NAME)]
    result = response(("K1", "K2"))
    result["resolutions"] = result["resolutions"][:2]
    rows = [_setting(NAME, quote=FIRST), _setting(NAME, quote=SECOND)]
    resolved, updates, _ = trusted(rows, result, subjects=actual)
    assert not updates
    assert [row.binding.actual_character_id for row in resolved] == [
        s.actual_character_id for s in actual
    ]
    assert all(row.binding.provisional_subject_key is None for row in resolved)
    assert [
        member["targetRef"] for member in resolved[0].binding.book_identity_proof.wire()["members"]
    ] == [f"character:{s.actual_character_id}" for s in actual]


def test_actual_reconnect_cannot_omit_a_supplied_same_name_target():
    actual = [_known(NAME) for _ in range(3)]
    result = response(("K1", "K2"))
    result["resolutions"] = result["resolutions"][:2]
    rows = [_setting(NAME, quote=FIRST), _setting(NAME, quote=SECOND)]
    resolved, _, _ = trusted(rows, result, subjects=actual, continue_on_candidate_failure=True)
    assert all(row.binding.book_identity_proof is None for row in resolved)


def test_mixed_actual_and_new_members_remain_reviewable():
    actual = [_known(NAME)]
    rows = [_discovery(NAME, quote=SECOND), _setting(NAME, quote=FIRST)]
    result = {
        "resolutions": [
            {"candidate_ref": "C1", "target_ref": "D1"},
            {"candidate_ref": "C2", "target_ref": "K1"},
        ],
        "identity_groups": response(("K1", "D1"))["identity_groups"],
    }
    resolved, _, _ = trusted(rows, result, subjects=actual, continue_on_candidate_failure=True)
    assert all(row.binding.book_identity_proof is None for row in resolved)


@pytest.mark.parametrize("change", ["revision", "reference", "hash", "episode"])
def test_identity_context_cannot_be_reused_for_a_foreign_or_changed_source(change):
    ctx = identity_context()
    data = ctx.source.model_dump(by_alias=True, mode="json")
    if change == "revision":
        data["revisionId"] = str(uuid4())
    elif change == "reference":
        data.update(referenceTimeKind="EPISODE", referenceEpisodeNo=7)
    elif change == "hash":
        data["contentHash"] = "0" * 64
    else:
        data = {"sourceKind": "EPISODE", "episodeId": str(uuid4()), "episodeNo": 1}
    from app.schemas.analysis_source import WorkerAnalysisSource

    with pytest.raises(ComparisonValidationError):
        resolve(
            candidates(),
            response(),
            source=WorkerAnalysisSource.model_validate(data),
            book_identity_context=ctx,
        )


def test_retry_validates_fresh_typed_response_and_never_echoes_the_invalid_proof():
    from app.llm.responses import LlmTextResponse

    bad = response()
    bad["identity_groups"][0]["members"][0]["target_ref"] = "K999"

    class SequenceClient:
        def __init__(self):
            self.requests = []
            self.results = [bad, response()]

        async def create_text_response(self, **kwargs):
            self.requests.append(kwargs)
            return LlmTextResponse(text=json.dumps(self.results.pop(0)))

    client = SequenceClient()
    ctx = identity_context()
    resolved, _ = asyncio.run(
        resolve_ordered_character_subjects(
            client=client,
            model="offline-test-model",
            max_attempts=2,
            max_output_tokens=5000,
            context=SubjectResolutionChunkContext(None, TEXT, None),
            candidates=candidates(),
            subjects=[],
            episode_no=None,
            analysis_source=ctx.source,
            book_identity_context=ctx,
        )
    )
    assert len(client.requests) == 2 and all(row.binding.book_identity_proof for row in resolved)
    assert "K999" not in client.requests[-1]["user_prompt"]


def test_order_change_and_new_origin_bind_proof_to_fresh_discovery_refs():
    ctx = identity_context()
    first, _, _ = trusted(ctx=ctx)
    result = response(("D2", "D4"))
    result["resolutions"] = [
        {"candidate_ref": "C1", "target_ref": "D2"},
        {"candidate_ref": "C2", "target_ref": "D2"},
        {"candidate_ref": "C3", "target_ref": "D4"},
        {"candidate_ref": "C4", "target_ref": "D4"},
    ]
    rows = candidates()
    rows = [rows[2], rows[0], rows[3], rows[1]]
    next_ctx = replace(ctx, analysis_job_id=uuid4())
    next_rows, _, _ = trusted(rows, result, ctx=next_ctx)
    assert {r.binding.candidate_id for r in first}.isdisjoint(
        {r.binding.candidate_id for r in next_rows}
    )
    assert all(
        r.binding.book_identity_proof.wire()["sourceAnalysisJobId"] == str(next_ctx.analysis_job_id)
        for r in next_rows
    )
    assert all(r.candidate.evidence_spans[0].start_offset is not None for r in next_rows)


def test_no_book_proof_is_created_on_episode_response():
    client = RecordingClient({"resolutions": response()["resolutions"]})
    rows, _ = asyncio.run(
        resolve_ordered_character_subjects(
            client=client,
            model="offline-test-model",
            max_attempts=1,
            max_output_tokens=5000,
            context=SubjectResolutionChunkContext(None, TEXT, None),
            candidates=candidates(),
            subjects=[],
            episode_no=1,
        )
    )
    assert all(row.binding.book_identity_proof is None for row in rows)
    assert "identity_groups" not in client.requests[0]["response_schema"].schema["properties"]


def save_with_fake_book_repository(monkeypatch, resolved, ctx, *, job=None):
    from app.schemas.worker import WorkerAnalysisJobPayload
    from app.services.setting_candidate_service import (
        SettingCandidateSaveItem,
        SettingCandidateService,
    )
    from app.worker.analysis_job_worker import _ordered_write_context
    from tests.test_setting_book_source_contract import book_payload_data
    from tests.test_setting_candidate_service import FakeSession, FakeSettingCandidateRepository

    data = book_payload_data()
    data["analysisJobId"] = str(job or ctx.analysis_job_id)
    data["settingBook"].update(
        contentHash=ctx.source.content_hash,
        referenceTimeKind=ctx.source.reference_time_kind,
        referenceEpisodeNo=ctx.source.reference_episode_no,
    )
    data["analysisContext"].update(
        sourceHash=ctx.source.content_hash,
        referenceTimeKind=ctx.source.reference_time_kind,
        referenceEpisodeNo=ctx.source.reference_episode_no,
    )
    data["settingBook"].update(
        settingBookId=str(ctx.source.setting_book_id), revisionId=str(ctx.source.revision_id)
    )
    payload = WorkerAnalysisJobPayload.model_validate(data)

    class Session(FakeSession):
        def scalars(self, statement):
            return {row.candidate.source_chunk_id for row in resolved}

    class Repository(FakeSettingCandidateRepository):
        def delete_by_setting_book_job(self, *args):
            self.deleted = args

    session = Session()
    repository = Repository(session)
    monkeypatch.setattr(
        "app.services.setting_candidate_service.fence_ordered_candidate_write", lambda *a, **k: None
    )
    service = SettingCandidateService(lambda: session, lambda session: repository)
    saved = service.replace_candidates_for_analysis_job(
        payload.work_id,
        payload.analysis_job_id,
        [
            SettingCandidateSaveItem(
                None,
                payload.setting_book.content_s3_key,
                row.candidate,
                row.binding,
                source_kind="SETTING_BOOK",
                setting_book_id=ctx.source.setting_book_id,
                setting_book_revision_id=ctx.source.revision_id,
            )
            for row in resolved
        ],
        [],
        _ordered_write_context(payload),
    )
    return saved, session


def test_actual_candidate_save_emits_exact_reserved_wire_subtree(monkeypatch):
    ctx = identity_context()
    resolved, _, _ = trusted(ctx=ctx)
    saved, session = save_with_fake_book_repository(monkeypatch, resolved, ctx)
    assert session.committed
    assert [row.raw_ai_result_json["setting_book_identity_proof"] for row in saved] == [
        row.binding.book_identity_proof.wire() for row in resolved
    ]
    assert all(row.episode_id is None for row in saved)


def test_candidate_save_rejects_proof_from_a_different_origin_before_writing(monkeypatch):
    ctx = identity_context()
    resolved, _, _ = trusted(ctx=ctx)
    with pytest.raises(ComparisonValidationError):
        save_with_fake_book_repository(monkeypatch, resolved, ctx, job=uuid4())


def test_stage_one_reserved_subtree_is_discarded_without_accepting_provider_ids():
    from app.analysis.setting_extractor import CharacterSettingExtractor
    from tests.test_setting_extractor import CHUNK_ID, DEFAULT_SCHEMA_HINTS

    result = {
        "candidates": [
            {
                "candidate_kind": "CHARACTER_DISCOVERY",
                "entity_type": "CHARACTER",
                "entity_name": NAME,
                "raw_entity_mention": None,
                "evidence_spans": [{"quote": FIRST, "start_offset": None, "end_offset": None}],
                "confidence": 0.99,
                "attribute_name": None,
                "attribute_value": None,
                "value_type": None,
                "value_json": None,
                "setting_book_identity_proof": {
                    "version": 1,
                    "parentTargetRef": f"character:{uuid4()}",
                },
            }
        ]
    }
    client = RecordingClient(result)
    extraction = asyncio.run(
        CharacterSettingExtractor(llm_client=client, model="offline-test-model").extract_from_chunk(
            source_chunk_id=CHUNK_ID, chunk_text=TEXT, schema_hints=DEFAULT_SCHEMA_HINTS
        )
    )
    assert len(extraction.candidates) == 1
    assert "setting_book_identity_proof" not in extraction.candidates[0].model_dump()


def test_multi_chunk_worker_resolves_full_origin_before_persisting_parent_proof():
    from app.analysis.character_subject_resolver import CharacterSubjectResolver
    from app.analysis.schemas import CharacterSettingExtractionResult
    from app.llm.responses import LlmTextResponse
    from app.models.setting_book_chunk import SettingBookChunk
    from app.schemas.worker import WorkerAnalysisJobPayload
    from app.services.setting_book_chunk_service import setting_book_char_count
    from app.services.setting_book_s3_chunking_service import SettingBookS3ChunkingService
    from app.worker.analysis_job_worker import AnalysisJobWorker
    from tests.test_setting_book_source_contract import book_payload_data
    from tests.test_setting_book_worker import BookSpring
    from tests.test_setting_extractor import DEFAULT_SCHEMA_HINTS

    ctx = identity_context()
    data = book_payload_data()
    data["analysisJobId"] = str(ctx.analysis_job_id)
    data["settingBook"].update(
        contentHash=ctx.source.content_hash,
        charCount=setting_book_char_count(TEXT),
        referenceTimeKind="WORK_START",
    )
    data["analysisContext"].update(
        sourceHash=ctx.source.content_hash, referenceTimeKind="WORK_START"
    )
    payload = WorkerAnalysisJobPayload.model_validate(data)
    split = TEXT.index(SECOND)
    chunks = [
        SettingBookChunk(
            id=uuid4(),
            analysis_job_id=payload.analysis_job_id,
            setting_book_revision_id=payload.setting_book.revision_id,
            chunk_index=i,
            start_offset=start,
            end_offset=end,
            chunk_text=TEXT[start:end],
            paragraph_start_index=i,
            paragraph_end_index=i,
        )
        for i, (start, end) in enumerate([(0, split), (split, len(TEXT))])
    ]
    rows = candidates()
    batches = [[rows[0], rows[2]], [rows[1], rows[3]]]
    for chunk, batch in zip(chunks, batches, strict=True):
        for row in batch:
            row.source_chunk_id = chunk.id

    class Extractor:
        async def extract_from_chunk(self, **kwargs):
            return CharacterSettingExtractionResult.model_construct(candidates=batches.pop(0))

    class Storage:
        def __init__(self):
            self.requests = []

        def get_text(self, key, version_id=None):
            self.requests.append((key, version_id))
            return TEXT

    final = response(("D1", "D3"))
    final["resolutions"] = [
        {"candidate_ref": f"C{i}", "target_ref": "D1" if i <= 2 else "D3"} for i in range(1, 5)
    ]

    class Provider:
        def __init__(self):
            self.requests = []
            self.results = [
                {
                    "resolutions": [
                        {"candidate_ref": "C1", "target_ref": "D1"},
                        {"candidate_ref": "C2", "target_ref": "D1"},
                    ]
                },
                final,
            ]

        async def create_text_response(self, **kwargs):
            self.requests.append(kwargs)
            return LlmTextResponse(text=json.dumps(self.results.pop(0)))

    class Save:
        def replace_candidates_for_analysis_job(self, **kwargs):
            self.items = kwargs["save_items"]
            return self.items

    class LocalIo:
        async def run(self, operation, *args, **kwargs):
            return operation(*args, **kwargs)

    storage = Storage()
    provider = Provider()
    save = Save()
    worker = AnalysisJobWorker(
        spring_client=BookSpring(),
        setting_extractor=Extractor(),
        subject_resolver=CharacterSubjectResolver(llm_client=provider, model="offline-test-model"),
        setting_book_chunking_service=SettingBookS3ChunkingService(storage, None),
        setting_candidate_service=save,
    )
    worker._blocking_io_executor = LocalIo()
    worker._owns_blocking_io_executor = False
    metrics = asyncio.run(
        worker._run_character_stage(payload, chunks, None, [], DEFAULT_SCHEMA_HINTS)
    )
    assert metrics["candidateCount"] == 4
    assert all(item.ordered_binding.book_identity_proof for item in save.items)
    assert storage.requests == [
        (payload.setting_book.content_s3_key, payload.setting_book.content_s3_version)
    ]
    assert len(provider.requests) == 2
    assert json.loads(provider.requests[-1]["user_prompt"])["context"]["frozen_source"] == TEXT
    assert all(item.candidate.evidence_spans[0].start_offset is not None for item in save.items)


def test_null_quote_repeated_inside_the_selected_parent_stays_unlocated():
    from app.analysis.setting_book_identity import BookIdentityContext

    profile = FIRST + "\r\n성별: 여자"
    text = TEXT.replace(FIRST, profile)
    rows = candidates()
    for row in rows[:2]:
        quote = row.evidence_spans[0].quote
        start = text.index(quote)
        row.evidence_spans[0].start_offset = start
        row.evidence_spans[0].end_offset = start + len(quote)
    result = response()

    def exact(quote):
        start = text.index(quote)
        return {"quote": quote, "start_offset": start, "end_offset": start + len(quote)}

    group = result["identity_groups"][0]
    group["members"][0]["profile_evidence"] = exact(profile)
    group["members"][1]["profile_evidence"] = exact(SECOND)
    group["distinct_evidence_spans"] = [exact(DISTINCT)]
    source = identity_context().source.model_copy(
        update={"content_hash": sha256(text.encode()).hexdigest()}
    )
    ctx = BookIdentityContext(uuid4(), source, text)
    resolved, _, _ = trusted(rows, result, ctx=ctx, continue_on_candidate_failure=True)
    assert resolved[2].binding.book_identity_proof is None
    assert resolved[2].binding.match_status == "AMBIGUOUS"
    assert resolved[2].binding.provisional_subject_key is None
    assert resolved[2].binding.actual_character_id is None
    assert resolved[2].binding.subject_failure_code is None
    assert all(row.binding.book_identity_proof for index, row in enumerate(resolved) if index != 2)
    assert resolved[2].candidate.evidence_spans[0].start_offset is None


def test_actual_alias_collision_cannot_be_omitted_from_a_new_partition():
    actual = [_known("보리", aliases=(NAME,))]
    resolved, _, provider = trusted(subjects=actual, continue_on_candidate_failure=True)
    assert all(row.binding.book_identity_proof is None for row in resolved)
    assert all(row.binding.match_status == "AMBIGUOUS" for row in resolved)
    assert len(provider.requests) == 1


def test_same_person_registered_alias_can_keep_existing_actual_binding():
    actual = [_known("보리", aliases=(NAME,))]
    result = response()
    result["identity_groups"] = []
    for row in result["resolutions"]:
        row["target_ref"] = "K1"
    resolved, _, _ = trusted(result=result, subjects=actual)
    assert all(row.binding.actual_character_id == actual[0].actual_character_id for row in resolved)
    assert all(row.binding.book_identity_proof is None for row in resolved)


def test_partial_repeated_discovery_cannot_create_an_incomplete_origin_proof():
    rows = candidates()
    third = _discovery(NAME, quote="나이: 28")
    quote = third.evidence_spans[0].quote
    start = TEXT.index(quote)
    third.evidence_spans[0].start_offset = start
    third.evidence_spans[0].end_offset = start + len(quote)
    rows.append(third)
    result = response()
    result["resolutions"].append({"candidate_ref": "C5", "target_ref": "D1"})
    resolved, _, _ = trusted(rows, result, continue_on_candidate_failure=True)
    assert all(row.binding.book_identity_proof is None for row in resolved)


def test_book_identity_response_passes_the_strict_provider_schema_boundary():
    import httpx

    from app.llm.openai_client import OpenAIResponsesClient
    from tests.test_book_world_provider_schema import _strict_object_violations

    async def scenario():
        requests = []

        async def provider(request):
            body = json.loads(request.content)
            requests.append(body)
            violations = _strict_object_violations(body["text"]["format"]["schema"])
            if violations:
                return httpx.Response(
                    400,
                    json={
                        "error": {
                            "code": "invalid_json_schema",
                            "type": "invalid_request_error",
                            "param": "text.format.schema",
                            "message": "Strict object schema is incomplete.",
                        }
                    },
                )
            return httpx.Response(
                200,
                json={
                    "status": "completed",
                    "output_text": json.dumps(response()),
                    "usage": {"input_tokens": 10, "output_tokens": 20},
                },
            )

        client = OpenAIResponsesClient(
            api_key="offline-test",
            model="offline-test-model",
            responses_api_url="https://provider.invalid/v1/responses",
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(provider)),
        )
        ctx = identity_context()
        try:
            resolved, _ = await resolve_ordered_character_subjects(
                client=client,
                model="offline-test-model",
                max_attempts=1,
                max_output_tokens=5000,
                context=SubjectResolutionChunkContext(None, TEXT, None),
                candidates=candidates(),
                subjects=[],
                episode_no=None,
                analysis_source=ctx.source,
                book_identity_context=ctx,
            )
            assert all(row.binding.book_identity_proof for row in resolved)
            assert len(requests) == 1
        finally:
            await client.aclose()

    asyncio.run(scenario())


def test_failed_partition_saves_discoveries_for_review_instead_of_failing_the_job(monkeypatch):
    ctx = identity_context()
    result = response()
    result["identity_groups"][0]["members"][0]["target_ref"] = "K999"
    resolved, _, _ = trusted(result=result, ctx=ctx, continue_on_candidate_failure=True)
    saved, session = save_with_fake_book_repository(monkeypatch, resolved, ctx)
    assert len(saved) == 4 and session.committed
    assert all(row.match_status == "AMBIGUOUS" for row in saved)
    assert all("setting_book_identity_proof" not in row.raw_ai_result_json for row in saved)


@pytest.mark.parametrize(
    "kind,number", [("UNSPECIFIED", None), ("WORK_START", None), ("EPISODE", 7)]
)
def test_saved_proof_keeps_the_exact_frozen_reference_pair(monkeypatch, kind, number):
    source = book_source(kind, number).model_copy(
        update={"content_hash": sha256(TEXT.encode()).hexdigest()}
    )
    ctx = identity_context(source)
    resolved, _, _ = trusted(ctx=ctx)
    saved, _ = save_with_fake_book_repository(monkeypatch, resolved, ctx)
    assert all(
        row.raw_ai_result_json["setting_book_identity_proof"]["referenceTimeKind"] == kind
        for row in saved
    )
    assert all(
        row.raw_ai_result_json["setting_book_identity_proof"]["referenceEpisodeNo"] == number
        for row in saved
    )


def test_book_output_truncation_expands_once_under_the_existing_configured_cap():
    from app.llm.exceptions import LlmOutputTruncatedError
    from app.llm.responses import LlmTextResponse

    class Client:
        def __init__(self):
            self.requests = []

        async def create_text_response(self, **kwargs):
            self.requests.append(kwargs)
            if len(self.requests) == 1:
                raise LlmOutputTruncatedError(
                    "Offline truncated structured response.",
                    max_output_tokens=kwargs["max_output_tokens"],
                    output_token_count=kwargs["max_output_tokens"],
                    incomplete_reason="max_output_tokens",
                )
            return LlmTextResponse(text=json.dumps(response()))

    client = Client()
    ctx = identity_context()
    rows, _ = asyncio.run(
        resolve_ordered_character_subjects(
            client=client,
            model="offline-test-model",
            max_attempts=1,
            max_output_tokens=5000,
            truncation_retry_max_output_tokens=12000,
            context=SubjectResolutionChunkContext(None, TEXT, None),
            candidates=candidates(),
            subjects=[],
            episode_no=None,
            analysis_source=ctx.source,
            book_identity_context=ctx,
        )
    )
    assert [request["max_output_tokens"] for request in client.requests] == [5000, 12000]
    assert all(row.binding.book_identity_proof for row in rows)


@pytest.mark.parametrize(
    "name,group_key", [("Straße", "straße"), ('"White"', '"white"'), ("A\u00a0B", "a\u00a0b")]
)
def test_proof_name_key_matches_java_character_group_contract(name, group_key):
    from app.analysis.setting_book_identity import BookIdentityContext

    text = TEXT.replace(NAME, name)
    rows = candidates()
    result = response()
    for row in rows:
        row.entity_name = name
        if row.raw_entity_mention:
            row.raw_entity_mention = row.raw_entity_mention.replace(NAME, name)
        for evidence in row.evidence_spans:
            evidence.quote = evidence.quote.replace(NAME, name)
            if evidence.start_offset is not None:
                evidence.start_offset = text.index(evidence.quote)
                evidence.end_offset = evidence.start_offset + len(evidence.quote)
    group = result["identity_groups"][0]
    group["normalized_name"] = group_key
    for member in group["members"]:
        evidence = member["profile_evidence"]
        evidence["quote"] = evidence["quote"].replace(NAME, name)
        evidence["start_offset"] = text.index(evidence["quote"])
        evidence["end_offset"] = evidence["start_offset"] + len(evidence["quote"])
    evidence = group["distinct_evidence_spans"][0]
    evidence["quote"] = evidence["quote"].replace(NAME, name)
    evidence["start_offset"] = text.index(evidence["quote"])
    evidence["end_offset"] = evidence["start_offset"] + len(evidence["quote"])
    source = identity_context().source.model_copy(
        update={"content_hash": sha256(text.encode()).hexdigest()}
    )
    ctx = BookIdentityContext(uuid4(), source, text)
    resolved, _, provider = trusted(rows, result, ctx=ctx)
    assert all(
        row.binding.book_identity_proof.wire()["normalizedName"] == group_key for row in resolved
    )
    assert all(
        target["normalized_name"] == group_key
        for target in json.loads(provider.requests[0]["user_prompt"])["targets"]
    )
