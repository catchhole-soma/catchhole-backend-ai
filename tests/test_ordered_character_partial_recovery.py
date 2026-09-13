"""Controlled provider responses verify partial recovery, never real model calls."""
import asyncio
import json
import socket
from types import SimpleNamespace

import pytest

from app.analysis import character_fact_comparator as module
from app.analysis.character_fact_comparison_pipeline import execute_character_fact_comparison_batch
from app.analysis.character_fact_projection import CharacterProjectionEntry
from app.analysis.exceptions import OrderedInputContextError
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.llm.responses import LlmTextResponse
from tests.test_ordered_character_slot_feedback import candidate, decision, initial


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("No network in controlled recovery tests")
    monkeypatch.setattr(socket.socket, "connect", denied)
    monkeypatch.setattr(module, "get_settings", lambda: SimpleNamespace())
    monkeypatch.setattr(module, "_estimate_prompt_tokens", lambda *args: 100)


def execute(sources, outputs, *, entries=None, fact_type="PROFILE"):
    calls = []
    class Client:
        async def create_text_response(self, **kwargs):
            calls.append(kwargs)
            output = outputs[len(calls) - 1]
            if isinstance(output, Exception):
                raise output
            return LlmTextResponse(text=json.dumps({"decisions": output}))
    comparator = module.CharacterFactComparator(
        llm_client=Client(), model="offline", max_attempts=3, max_output_tokens=3000,
        batch_max_output_tokens=16000, batch_max_input_tokens=64000, batch_max_candidates=10,
    )
    async def run():
        return await execute_character_fact_comparison_batch(
            comparator, matched_character_name="비요른", canonical_fact_type=fact_type,
            candidates=sources, snapshot_entries=initial() if entries is None else entries,
            ordered_context=True,
        )
    try:
        return asyncio.run(run()), calls
    except Exception as error:
        return error, calls


@pytest.mark.parametrize("recover", [True, False])
def test_only_rejected_candidate_is_retried_once_and_independent_success_is_kept(recover):
    sources = [candidate("C1", "profile.occupation"), candidate("C2", "profile.attribute")]
    invalid = [decision(row) for row in sources]
    repaired = decision(sources[1], "MERGE", "P1") if recover else invalid[1]
    before = [row.model_dump() for row in sources]
    result, calls = execute(sources, [invalid, invalid, invalid, [repaired]])
    assert not isinstance(result, Exception), result
    assert len(calls) == 4
    assert [row["candidate_ref"] for row in json.loads(calls[-1]["user_prompt"])["candidates"]] == ["C2"]
    assert [row.candidate_ref for row in result.decisions] == (["C1", "C2"] if recover else ["C1"])
    assert [row.candidate_ref for row in result.failures] == ([] if recover else ["C2"])
    assert result.singleton_fallback_count == 1
    assert [row.model_dump() for row in sources] == before


def test_later_same_key_is_reconsidered_and_never_uses_the_failed_q():
    sources = [candidate(f"C{i}", "profile.attribute") for i in range(1, 4)]
    invalid = [decision(sources[0], "UPDATE", "P1"), decision(sources[1]),
               decision(sources[2], "MERGE", "Q2")]
    final = decision(sources[2], "MERGE", "Q1")
    result, calls = execute(sources, [invalid] * 3 + [[invalid[1]], [final]])
    assert not isinstance(result, Exception), result
    assert len(calls) == 5
    assert [row.candidate_ref for row in result.decisions] == ["C1", "C3"]
    assert [row.candidate_ref for row in result.failures] == ["C2"]
    assert result.decisions[1].target_snapshot_ref == "Q1"
    assert result.decisions[1].dependency_candidate_refs == ["C1"]
    final_input = json.loads(calls[-1]["user_prompt"])
    assert [row["ref"] for row in final_input["snapshot_entries"]] == ["Q1"]


def test_malformed_item_preserves_other_independent_items_but_bad_coverage_preserves_none():
    sources = [candidate("C1", "profile.occupation"), candidate("C2", "profile.attribute")]
    invalid = [decision(sources[0]), {**decision(sources[1]), "operation": "INVALID"}]
    repaired = decision(sources[1], "REVIEW_REQUIRED")
    result, calls = execute(sources, [invalid] * 3 + [[repaired]])
    assert not isinstance(result, Exception), result
    assert len(calls) == 4 and len(result.decisions) == 2
    bad_order = list(reversed(invalid))
    result, calls = execute(sources, [bad_order] * 3 + [[invalid[0]], [repaired]])
    assert not isinstance(result, Exception), result
    assert len(calls) == 5 and result.singleton_fallback_count == 2


def test_failed_status_blocks_retention_of_all_later_statuses_even_another_key():
    sources = [candidate("C1", "status.부상"), candidate("C2", "status.회복")]
    entries = [CharacterProjectionEntry("P1", "STATUS", "status.부상", "부상", {"value": "부상"},
                                       provenance=initial()[0].provenance)]
    invalid = [decision(sources[0]), decision(sources[1])]
    reviewed = decision(sources[1], "REVIEW_REQUIRED")
    result, calls = execute(sources, [invalid] * 3 + [[invalid[0]], [reviewed]],
                            entries=entries, fact_type="STATUS")
    assert not isinstance(result, Exception), result
    assert len(calls) == 5
    assert [row.candidate_ref for row in result.failures] == ["C1"]
    assert result.decisions[0].operation == "REVIEW_REQUIRED"


@pytest.mark.parametrize("error", [AiTokenQuotaExhaustedError(), OrderedInputContextError("fixed input")])
def test_recovery_execution_errors_abort_instead_of_returning_retained_success(error):
    sources = [candidate("C1", "profile.occupation"), candidate("C2", "profile.attribute")]
    invalid = [decision(row) for row in sources]
    result, calls = execute(sources, [invalid] * 3 + [error])
    assert result is error
    assert len(calls) == 4


def test_failed_first_fixed_key_does_not_discard_a_later_independent_key():
    sources = [candidate("C1", "profile.attribute"), candidate("C2", "profile.occupation")]
    invalid = [decision(row) for row in sources]
    result, calls = execute(sources, [invalid] * 3 + [[invalid[0]]])
    assert not isinstance(result, Exception), result
    assert len(calls) == 4
    assert [row.candidate_ref for row in result.decisions] == ["C2"]
    assert [row.candidate_ref for row in result.failures] == ["C1"]


def test_retained_remove_and_readd_keep_the_absence_dependency():
    sources = [candidate("C1", "status.회복"), candidate("C2", "status.부상"),
               candidate("C3", "status.두통")]
    entries = [CharacterProjectionEntry("P1", "STATUS", "status.부상", "부상", {"value": "부상"},
                                       provenance=initial()[0].provenance)]
    remove = {**decision(sources[0], "REMOVE"), "removed_snapshot_refs": ["P1"]}
    invalid = [remove, decision(sources[1]), decision(sources[2], "UPDATE", "P999")]
    result, calls = execute(sources, [invalid] * 3 + [[invalid[-1]]], entries=entries, fact_type="STATUS")
    assert not isinstance(result, Exception), result
    assert len(calls) == 4
    assert [row.candidate_ref for row in result.decisions] == ["C1", "C2"]
    assert result.decisions[1].dependency_candidate_refs == ["C1"]
    assert [row.candidate_ref for row in result.failures] == ["C3"]


def test_missing_input_provenance_is_not_a_recoverable_response_error():
    sources = [candidate("C1", "profile.attribute"), candidate("C2", "profile.occupation")]
    entries = [CharacterProjectionEntry("P1", "PROFILE", "profile.attribute", "원래 값", None)]
    result, calls = execute(sources, [], entries=entries)
    assert isinstance(result, OrderedInputContextError)
    assert calls == []
