"""Rule-bound aliases outside provider coverage still lose unowned BOOK identity."""

import json
from dataclasses import replace
from hashlib import sha256

import pytest

from app.analysis.ordered_character_subjects import OrderedSubjectResolutionMetrics
from tests.test_book_identity_child_isolation import assert_review_hold, external_child
from tests.test_book_identity_producer import FIRST, SECOND, response, save_with_fake_book_repository, trusted
from tests.test_ordered_subject_rules import _known, _setting
from tests.test_ordered_subject_rules import no_network as no_network


def rule_alias(*, inside=False):
    ctx, outside = external_child()
    alias = (_setting("은등", raw="은등", quote=FIRST) if inside
             else outside.model_copy(update={"entity_name": "은등", "raw_entity_mention": "은등"}))
    subjects = [replace(_known("백솔"), aliases=("은등",)), _known("백솔")]
    result = response(("K1", "K2"))
    result["resolutions"] = [{"candidate_ref": "C1", "target_ref": "K1"}]
    return ctx, [_setting("백솔", quote=FIRST), alias], subjects, result


@pytest.mark.parametrize("bad", ["outside", "wrong_parent", "partial_offsets", "wrong_slice"])
def test_rule_alias_unowned_child_is_cleared_even_when_provider_does_not_return_it(bad):
    ctx, candidates, subjects, result = rule_alias()
    evidence = candidates[1].evidence_spans[0]
    if bad == "wrong_parent":
        evidence.quote = SECOND
        evidence.start_offset = ctx.source_text.index(SECOND)
        evidence.end_offset = evidence.start_offset + len(SECOND)
    elif bad == "partial_offsets":
        evidence.end_offset = None
    elif bad == "wrong_slice":
        evidence.start_offset = 0
        evidence.end_offset = len(evidence.quote)
    original = candidates[1].model_dump()
    original_targets = list(subjects)
    metrics = OrderedSubjectResolutionMetrics()
    rows, updates, client = trusted(candidates, result, ctx=ctx, subjects=subjects, metrics=metrics,
                                    continue_on_candidate_failure=True)
    assert [item["ref"] for item in json.loads(client.requests[0]["user_prompt"])["candidates"]] == ["C1"]
    assert rows[0].binding.actual_character_id == subjects[0].actual_character_id
    assert rows[0].binding.book_identity_proof is not None
    assert_review_hold(rows[1])
    assert rows[1].candidate.model_dump() == original
    assert subjects == original_targets and updates == []
    assert metrics.llm_call_count == 1 and metrics.llm_failed_count == 0
    # The provider answered one row. Holding a rule row must not increase this denominator.
    assert (metrics.llm_resolved_count, metrics.llm_unresolved_count) == (1, 0)
    assert sum(row.binding.match_status == "MATCHED" for row in rows) == 1


def test_owned_rule_alias_keeps_actual_binding_and_proof_without_inflating_provider_metrics():
    ctx, candidates, subjects, result = rule_alias(inside=True)
    metrics = OrderedSubjectResolutionMetrics()
    rows, _, client = trusted(candidates, result, ctx=ctx, subjects=subjects, metrics=metrics)
    assert len(client.requests) == 1
    assert all(row.binding.actual_character_id == subjects[0].actual_character_id for row in rows)
    assert all(row.binding.book_identity_proof is not None for row in rows)
    assert all(row.binding.subject_failure_code is None for row in rows)
    assert (metrics.llm_resolved_count, metrics.llm_unresolved_count) == (1, 0)
    assert sum(row.binding.match_status == "MATCHED" for row in rows) == 2


def test_provider_null_and_rule_hold_both_stay_unbound_but_only_provider_row_is_counted():
    ctx, candidates, subjects, result = rule_alias()
    result["resolutions"][0]["target_ref"] = None
    metrics = OrderedSubjectResolutionMetrics()
    rows, _, _ = trusted(candidates, result, ctx=ctx, subjects=subjects, metrics=metrics)
    assert all(row.binding.actual_character_id is None for row in rows)
    assert all(row.binding.match_status == "AMBIGUOUS" for row in rows)
    assert (metrics.llm_resolved_count, metrics.llm_unresolved_count, metrics.llm_failed_count) == (0, 1, 0)


def test_rule_hold_persists_waiting_wire_without_actual_or_provisional_target(monkeypatch):
    ctx, candidates, subjects, result = rule_alias()
    rows, _, _ = trusted(candidates, result, ctx=ctx, subjects=subjects)
    saved, session = save_with_fake_book_repository(monkeypatch, rows, ctx)
    assert session.committed
    assert "setting_book_identity_proof" in saved[0].raw_ai_result_json
    held = saved[1]
    assert held.match_status == "AMBIGUOUS" and held.comparison_status == "WAITING_FOR_CHARACTER_MATCH"
    assert held.review_status == "PENDING_REVIEW"
    assert held.matched_character_id is None and held.provisional_subject_key is None
    assert held.comparison_failure_code is None and held.preparation_failure_stage is None
    assert "setting_book_identity_proof" not in held.raw_ai_result_json
    assert held.raw_ai_result_json["orderedSubjectResolution"] == {
        "actualCharacterId": None, "provisionalSubjectKey": None, "matchStatus": "AMBIGUOUS"}


def test_healthy_independent_rule_target_survives_and_final_rule_count_excludes_held_alias():
    ctx, candidates, subjects, result = rule_alias()
    quote = "[인물: 민하]\r\n레벨: 12"
    text = ctx.source_text + "\r\n\r\n" + quote
    ctx = replace(ctx, source_text=text,
                  source=ctx.source.model_copy(update={"content_hash": sha256(text.encode()).hexdigest()}))
    subjects.append(_known("민하"))
    candidates.append(_setting("민하", raw="민하", quote=quote))
    metrics = OrderedSubjectResolutionMetrics()
    rows, _, client = trusted(candidates, result, ctx=ctx, subjects=subjects, metrics=metrics)
    queried = {item["ref"] for item in json.loads(client.requests[0]["user_prompt"])["candidates"]}
    assert queried == {"C1"}
    assert_review_hold(rows[1])
    assert rows[2].binding.actual_character_id == subjects[2].actual_character_id
    actual_rule_matches = [row for i, row in enumerate(rows, 1)
                           if f"C{i}" not in queried and row.binding.match_status == "MATCHED"]
    assert len(actual_rule_matches) == 1
    assert metrics.llm_resolved_count + metrics.llm_unresolved_count == len(queried)
