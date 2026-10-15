"""Local BOOK child holds versus global partition failures; all calls are offline.

Round2 fixtures copy every public extraction boundary row, without importing gold
or pretending to recover the actual provider's unavailable selection response.
"""

import asyncio
import copy
import json
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.analysis.character_subject_resolver import SubjectResolutionChunkContext
from app.analysis.ordered_character_subjects import (
    OrderedSubjectResolutionMetrics,
    OrderedSubjectTarget,
    _rule_selection,
    resolve_ordered_character_subjects,
)
from app.analysis.schemas import ExtractedSettingCandidate
from app.analysis.setting_book_identity import BookIdentityContext
from app.schemas.analysis_source import WorkerAnalysisSource
from tests.test_book_identity_producer import (
    FIRST, SECOND, TEXT, candidates, identity_context, response,
    save_with_fake_book_repository, trusted,
)
from tests.test_ordered_subject_rules import RecordingClient, _known, _setting
from tests.test_ordered_subject_rules import no_network as no_network


def external_child():
    quote = "[미정 구상]\r\n기록관은 언젠가 시력을 잃을 수도 있다."
    text = TEXT + "\r\n\r\n" + quote
    ctx = identity_context()
    ctx = replace(ctx, source=ctx.source.model_copy(update={"content_hash": sha256(text.encode()).hexdigest()}),
                  source_text=text)
    row = _setting("백솔", raw="기록관 백솔", quote=quote)
    row.evidence_spans[0].start_offset = text.index(quote)
    row.evidence_spans[0].end_offset = len(text)
    return ctx, row


def assert_review_hold(row):
    assert row.binding.match_status == "AMBIGUOUS"
    assert row.binding.actual_character_id is None
    assert row.binding.provisional_subject_key is None
    assert row.binding.book_identity_proof is None
    assert row.binding.subject_failure_code is None


def test_valid_partition_external_child_does_not_discard_normal_profiles_or_retry():
    ctx, outside = external_child()
    rows, result = candidates() + [outside], response()
    result["resolutions"].append({"candidate_ref": "C5", "target_ref": "D1"})
    original = outside.model_dump()
    metrics = OrderedSubjectResolutionMetrics()
    resolved, updates, provider = trusted(rows, result, ctx=ctx, metrics=metrics, continue_on_candidate_failure=True)
    assert all(row.binding.book_identity_proof is not None for row in resolved[:4])
    assert_review_hold(resolved[4])
    assert resolved[4].candidate.model_dump() == original
    assert len(updates) == 2 and all(outside.evidence_spans[0].quote not in target.evidence for target in updates)
    assert len(provider.requests) == 1
    assert metrics.llm_resolved_count == 4 and metrics.llm_unresolved_count == 1 and metrics.llm_failed_count == 0


@pytest.mark.parametrize("invalid", ["wrong_parent", "partial_offsets", "bad_slice", "empty_range"])
def test_invalid_child_slice_is_a_local_review_hold(invalid):
    rows = candidates()
    evidence = rows[2].evidence_spans[0]
    if invalid == "wrong_parent":
        evidence.quote = "나이: 34"
        evidence.start_offset = TEXT.index(evidence.quote)
        evidence.end_offset = evidence.start_offset + len(evidence.quote)
    elif invalid == "partial_offsets":
        evidence.start_offset = TEXT.index(evidence.quote)
    elif invalid == "bad_slice":
        evidence.start_offset, evidence.end_offset = 0, len(evidence.quote)
    else:
        evidence.start_offset = evidence.end_offset = 0
    resolved, _, _ = trusted(rows, response(), continue_on_candidate_failure=True)
    assert_review_hold(resolved[2])
    assert all(row.binding.book_identity_proof for i, row in enumerate(resolved) if i != 2)


def test_actual_target_is_removed_from_unowned_child_without_dropping_valid_actual_sibling():
    ctx, outside = external_child()
    actual = [_known("백솔"), _known("백솔")]
    result = response(("K1", "K2"))
    result["resolutions"] = [{"candidate_ref": "C1", "target_ref": "K1"},
                              {"candidate_ref": "C2", "target_ref": "K1"}]
    rows = [_setting("백솔", quote=FIRST), outside]
    resolved, updates, _ = trusted(rows, result, ctx=ctx, subjects=actual, continue_on_candidate_failure=True)
    assert resolved[0].binding.actual_character_id == actual[0].actual_character_id
    assert resolved[0].binding.book_identity_proof is not None
    assert_review_hold(resolved[1])
    assert updates == []


@pytest.mark.parametrize("invalid", ["bad_discovery", "incomplete_partition", "overlap", "missing_coverage", "invented_target"])
def test_invalid_partition_or_discovery_remains_a_global_validation_failure(invalid):
    ctx, outside = external_child()
    rows, result = candidates() + [outside], response()
    result["resolutions"].append({"candidate_ref": "C5", "target_ref": "D1"})
    group = result["identity_groups"][0]
    if invalid == "bad_discovery":
        rows[0].evidence_spans = copy.deepcopy(outside.evidence_spans)
    elif invalid == "incomplete_partition":
        group["members"].pop()
    elif invalid == "overlap":
        group["members"][1]["profile_evidence"] = copy.deepcopy(group["members"][0]["profile_evidence"])
    elif invalid == "missing_coverage":
        result["resolutions"].pop()
    else:
        result["resolutions"][-1]["target_ref"] = "K999"
    resolved, _, _ = trusted(rows, result, ctx=ctx, continue_on_candidate_failure=True)
    assert all(row.binding.book_identity_proof is None for row in resolved)
    assert all(row.binding.match_status == "AMBIGUOUS" for row in resolved)
    assert all(row.binding.subject_failure_code is not None for row in resolved[2:])


def test_held_child_persists_normal_waiting_state_without_reserved_proof_or_target(monkeypatch):
    ctx, outside = external_child()
    result = response()
    result["resolutions"].append({"candidate_ref": "C5", "target_ref": "D1"})
    resolved, _, _ = trusted(candidates() + [outside], result, ctx=ctx, continue_on_candidate_failure=True)
    saved, session = save_with_fake_book_repository(monkeypatch, resolved, ctx)
    assert session.committed
    assert all("setting_book_identity_proof" in row.raw_ai_result_json for row in saved[:4])
    held = saved[4]
    assert held.match_status == "AMBIGUOUS"
    assert held.comparison_status == "WAITING_FOR_CHARACTER_MATCH"
    assert held.comparison_failure_code is None and held.preparation_failure_stage is None
    assert held.review_status == "PENDING_REVIEW"
    assert held.provisional_subject_key is None and held.matched_character_id is None
    assert "setting_book_identity_proof" not in held.raw_ai_result_json
    wire = held.raw_ai_result_json["orderedSubjectResolution"]
    assert wire == {"provisionalSubjectKey": None, "actualCharacterId": None, "matchStatus": "AMBIGUOUS"}


def actual_boundary(group):
    data = json.loads((Path(__file__).parent / "fixtures/book_identity_round2" / (group + ".json")).read_text())
    rows = [ExtractedSettingCandidate.model_validate(item["extraction"]) for item in data["candidates"]]
    ids = [UUID(item["candidate_id"]) for item in data["candidates"]]
    source = WorkerAnalysisSource.model_validate(data["source"])
    ctx = BookIdentityContext(UUID(data["sourceAnalysisJobId"]), source, data["sourceText"])
    discoveries = [(index, row) for index, row in enumerate(rows, 1) if row.candidate_kind == "CHARACTER_DISCOVERY"]
    targets = {f"D{index}": OrderedSubjectTarget(name=row.entity_name, provisional_subject_key=f"provisional-character:{ids[index - 1]}")
               for index, row in discoveries}
    homonyms = [(index, row) for index, row in discoveries if row.entity_name == rows[0].entity_name]
    refs = [f"D{index}" for index, _ in homonyms]
    resolutions, held = [], []
    for index, row in enumerate(rows, 1):
        if _rule_selection(row, targets) is not None:
            continue
        owner = None
        for ref, parent in zip(refs, data["parentProfiles"], strict=True):
            if all((parent["quote"].count(span.quote) == 1 if span.start_offset is None and span.end_offset is None
                    else span.start_offset is not None and span.end_offset is not None
                    and parent["start_offset"] <= span.start_offset < span.end_offset <= parent["end_offset"])
                   for span in row.evidence_spans):
                owner = ref
        if owner is None:
            held.append(index - 1)
        # Deliberately simulate an unowned selection; the actual response was not preserved.
        resolutions.append({"candidate_ref": f"C{index}", "target_ref": owner or refs[0]})
    result = {"resolutions": resolutions, "identity_groups": [{"group_ref": "G1", "decision": "DISTINCT_IDENTITIES",
        "normalized_name": homonyms[0][1].entity_name,
        "members": [{"target_ref": ref, "profile_evidence": parent} for ref, parent in zip(refs, data["parentProfiles"], strict=True)],
        "distinct_evidence_spans": [data["distinctEvidence"]]}]}
    return data, rows, ids, ctx, result, held


@pytest.mark.parametrize("group, count, held_count", [("A", 56, 1), ("C", 44, 3)])
def test_actual_whole_origin_preserves_six_discoveries_and_all_valid_profile_rows(group, count, held_count, monkeypatch):
    data, rows, ids, ctx, result, held = actual_boundary(group)
    assert len(rows) == data["count"] == count and len(held) == held_count
    original = [row.model_dump() for row in rows]
    client = RecordingClient(result)
    resolved, updates = asyncio.run(resolve_ordered_character_subjects(
        client=client, model="offline-actual-boundary-test", max_attempts=2, max_output_tokens=10000,
        context=SubjectResolutionChunkContext(None, ctx.source_text, None), candidates=rows,
        subjects=[], episode_no=None, analysis_source=ctx.source, book_identity_context=ctx,
        candidate_ids=ids, continue_on_candidate_failure=True,
    ))
    assert len(resolved) == count and len(client.requests) == 1 and len(updates) == 6
    assert [row.binding.candidate_id for row in resolved] == ids
    assert len({row.binding.provisional_subject_key for row in resolved if row.candidate.candidate_kind == "CHARACTER_DISCOVERY"}) == 6
    for index in held:
        assert_review_hold(resolved[index])
        assert resolved[index].candidate.model_dump() == original[index]
    for index, row in enumerate(resolved):
        if index not in held:
            assert row.binding.match_status == "MATCHED" and row.binding.subject_failure_code is None
            if row.candidate.entity_name == rows[0].entity_name:
                assert row.binding.book_identity_proof is not None
    saved, _ = save_with_fake_book_repository(monkeypatch, resolved, ctx)
    for index in held:
        assert saved[index].comparison_status == "WAITING_FOR_CHARACTER_MATCH"
        assert "setting_book_identity_proof" not in saved[index].raw_ai_result_json
        assert saved[index].matched_character_id is None and saved[index].provisional_subject_key is None
    held_quotes = {span.quote for index in held for span in rows[index].evidence_spans}
    assert not any(held_quotes.intersection(target.evidence) for target in updates)
