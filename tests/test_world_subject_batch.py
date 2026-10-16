"""Manuscript identity resolution uses ordered, validated state without paid calls."""

import asyncio
import json
from types import SimpleNamespace
from uuid import UUID

import httpx
import pytest

from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.world_setting_comparator import WorldSettingSubjectResolver
from app.clients.exceptions import AiTokenQuotaExhaustedError
from app.domain.enums import AnalysisFailureCode
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.responses import LlmTextResponse
from app.schemas.analysis_source import WorkerAnalysisSource
from app.schemas.worker import (
    WorkerWorldSettingSubject,
    WorkerWorldSettingSubjectResolutionCandidate,
)
from tests.test_setting_book_source_contract import SOURCE


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    class Encoding:
        def encode(self, text, **kwargs):
            return list(text.encode())
    monkeypatch.setattr("app.analysis.ordered_context.tiktoken.get_encoding", lambda *args: Encoding())
    monkeypatch.setattr("app.analysis.world_setting_comparator.get_settings", lambda: SimpleNamespace(
        llm_world_setting_batch_subject_resolution_max_output_tokens=32000,
        llm_world_setting_batch_subject_resolution_max_input_tokens=64000,
    ))


def candidate(index, name="백탑", *, category="LOCATION", quote=None, book=False, asserted=True):
    return WorkerWorldSettingSubjectResolutionCandidate(
        candidate_id=UUID(int=index), subject_name=name, category=category,
        source_episode_id=None if book else UUID(int=1000), source_episode_no=None if book else 4,
        source=WorkerAnalysisSource.model_validate(SOURCE) if book else None,
        evidence_spans=[{"quote": quote or f"{index}번 원문에서 {name}의 모습을 설명한다."}],
        raw_extraction_json={"epistemic_status": "ASSERTED" if asserted else "UNRESOLVED"},
    )


def target(index=100, name="백탑", *, quote="백탑은 동쪽에 있다.", aliases=()):
    return WorkerWorldSettingSubject(world_setting_id=UUID(int=index), subject_name=name,
                                    aliases=list(aliases), identity_evidence=[{"quote": quote}])


def row(ref, selected=(), *, ambiguous=False, proofs=()):
    return {"candidate_ref": ref, "selected_subject_refs": list(selected), "ambiguous": ambiguous,
            "identity_proofs": list(proofs)}


def result(*rows):
    return {"decisions": list(rows)}


class Client:
    def __init__(self, outputs):
        self.outputs, self.calls = list(outputs), []

    async def create_text_response(self, **kwargs):
        self.calls.append(kwargs)
        output = self.outputs.pop(0)
        if isinstance(output, Exception):
            raise output
        return LlmTextResponse(text=output if isinstance(output, str) else json.dumps(output))


def run(candidates, outputs, *, subjects=None, ordered=True, automatic=True, attempts=1, input_limit=64000):
    subjects = subjects if subjects is not None else {"LOCATION": []}
    client = Client(outputs)
    resolver = WorldSettingSubjectResolver(llm_client=client, model="gpt-6-luna", max_attempts=attempts,
                                          max_output_tokens=2000)
    resolver.batch_max_input_tokens = input_limit
    outcomes = asyncio.run(resolver.select_manuscript_subjects(
        candidates, subjects, ordered=ordered, continue_on_candidate_failure=automatic,
    ))
    return outcomes, client, subjects


def test_whole_manuscript_uses_one_request_and_replays_new_targets_in_input_order():
    candidates = [candidate(1), candidate(2), candidate(3, "붉은 탑"), candidate(4, "탑 기사단", category="FACTION")]
    before = [item.model_dump() for item in candidates]
    outcomes, client, subjects = run(candidates, [result(
        row("C1"), row("C2", ["N1"]), row("C3", ["S1"]), row("C4"),
    )], subjects={"LOCATION": [target(name="붉은 탑")], "FACTION": []})
    assert [item.candidate_id for item in outcomes] == [UUID(int=i) for i in range(1, 5)]
    assert [item.provisional_subject_keys for item in outcomes] == [
        [f"provisional-world:{UUID(int=1)}"], [f"provisional-world:{UUID(int=1)}"], [],
        [f"provisional-world:{UUID(int=4)}"],
    ]
    assert outcomes[2].target_world_setting_ids == [UUID(int=100)]
    assert len(client.calls) == 1
    request = client.calls[0]
    assert request["model"] == "gpt-6-luna" and request["max_output_tokens"] == 32000
    assert str(UUID(int=1)) not in request["user_prompt"]
    assert str(UUID(int=100)) not in request["user_prompt"]
    assert [item.model_dump() for item in candidates] == before
    assert [span.quote for span in subjects["LOCATION"][1].identity_evidence] == [
        candidates[0].evidence_spans[0].quote, candidates[1].evidence_spans[0].quote,
    ]
    from tests.test_book_world_provider_schema import _strict_object_violations
    assert _strict_object_violations(request["response_schema"].schema) == []


@pytest.mark.parametrize("bad_rows", [
    [row("C1")],
    [row("C2"), row("C1")],
    [row("C1"), row("C1")],
    [row("C1", ["N2"]), row("C2")],
    [row("C1", ["S2"]), row("C2")],
    [row("C1", ["S1", "S1"]), row("C2")],
    [row("C1", ["S1"]), row("C2", ["N1"])],
    [row("C1", ambiguous=True), row("C2", ["N1"])],
])
def test_invalid_coverage_or_refs_retry_before_state_is_applied(bad_rows):
    original = target()
    outcomes, client, subjects = run([candidate(1), candidate(2)], [
        result(*bad_rows), result(row("C1"), row("C2", ["N1"])),
    ], subjects={"LOCATION": [original], "FACTION": [target(101)]}, attempts=2)
    assert len(client.calls) == 2
    assert outcomes[0].provisional_subject_keys == outcomes[1].provisional_subject_keys
    assert subjects["LOCATION"][0] == original
    assert len(subjects["LOCATION"]) == 2
    assert "validation_feedback" in json.loads(client.calls[1]["user_prompt"])


def test_invalid_response_feedback_never_echoes_rejected_values(caplog):
    outcomes, client, _ = run([candidate(1)], [
        result(row("SECRET_CANDIDATE", ["SECRET_TARGET"])), result(row("C1", ambiguous=True)),
    ], attempts=2)
    assert outcomes[0].ambiguous
    assert "SECRET_" not in client.calls[-1]["user_prompt"] + caplog.text


def test_ambiguity_discards_valid_selection_without_enriching_or_creating_a_target():
    source = candidate(1, quote="보류한 근거")
    initial = target()
    outcomes, client, subjects = run([source, candidate(2)], [result(
        row("C1", ["S1"], ambiguous=True), row("C2", ["S1"]),
    )], subjects={"LOCATION": [initial]})
    assert outcomes[0].ambiguous and not outcomes[0].target_world_setting_ids
    assert outcomes[1].target_world_setting_ids == [initial.world_setting_id]
    assert len(subjects["LOCATION"]) == 1
    assert "보류한 근거" not in [span.quote for span in subjects["LOCATION"][0].identity_evidence]
    assert len(client.calls) == 1


def test_confirmed_only_exact_and_empty_shortcuts_leave_only_fuzzy_candidates_in_request():
    candidates = [candidate(1), candidate(2, "흰 탑"), candidate(3, category="FACTION")]
    subjects = {"LOCATION": [target(), target(101)], "FACTION": []}
    outcomes, client, _ = run(candidates, [result(row("C2", ["S1"]))], subjects=subjects, ordered=False)
    assert [item.target_world_setting_ids for item in outcomes] == [
        [UUID(int=100), UUID(int=101)], [UUID(int=100)], [],
    ]
    assert all(not item.provisional_subject_keys for item in outcomes)
    assert [item["candidate_ref"] for item in json.loads(client.calls[0]["user_prompt"])["candidates"]] == ["C2"]
    assert len(subjects["LOCATION"][0].identity_evidence) == 1


def test_confirmed_only_more_than_twenty_exact_matches_fails_before_provider_call():
    with pytest.raises(ComparisonValidationError, match="20"):
        run([candidate(1)], [], subjects={"LOCATION": [target(i) for i in range(100, 121)]}, ordered=False)


def test_confirmed_shortcuts_hide_irrelevant_categories_without_renumbering_targets():
    tower, stone = target(), target(101, "마석")
    candidates = [candidate(1), candidate(2, "흰 돌", category="IMPORTANT_ITEM")]
    outcomes, client, subjects = run(candidates, [result(row("C2", ["S2"]))],
        subjects={"LOCATION": [tower], "IMPORTANT_ITEM": [stone]}, ordered=False)
    request = json.loads(client.calls[0]["user_prompt"])
    assert [(item["ref"], item["category"]) for item in request["initial_subjects"]] == [
        ("S2", "IMPORTANT_ITEM"),
    ]
    assert [item.target_world_setting_ids for item in outcomes] == [[UUID(int=100)], [UUID(int=101)]]
    assert subjects == {"LOCATION": [tower], "IMPORTANT_ITEM": [stone]}


def test_oversized_mixed_categories_split_to_complete_relevant_context_without_false_failures():
    from app.analysis.ordered_context import ensure_ordered_prompt_fits

    candidates = [candidate(1), candidate(2, "마석", category="IMPORTANT_ITEM")]
    initial = [target(quote="a" * 5000), target(101, "마석", quote="b" * 5000)]
    single_counts = []
    for source, subject in zip(candidates, initial, strict=True):
        _, probe, _ = run([source], [result(row("C1", ["S1"]))],
                          subjects={source.category: [subject]})
        request = probe.calls[0]
        single_counts.append(ensure_ordered_prompt_fits(
            request["system_prompt"], request["user_prompt"], response_schema=request["response_schema"],
        ))
    outcomes, client, subjects = run(candidates, [result(row("C1", ["S1"])), result(row("C2", ["S2"]))],
        subjects={"LOCATION": [initial[0]], "IMPORTANT_ITEM": [initial[1]]}, input_limit=max(single_counts) + 100)
    assert all(item.failure_code is None for item in outcomes)
    assert [item.target_world_setting_ids for item in outcomes] == [[UUID(int=100)], [UUID(int=101)]]
    requests = [json.loads(call["user_prompt"]) for call in client.calls]
    assert [[item["ref"] for item in request["initial_subjects"]] for request in requests] == [["S1"], ["S2"]]
    assert [request["initial_subjects"][0]["identity_evidence"] for request in requests] == [["a" * 5000], ["b" * 5000]]
    assert len(subjects["LOCATION"][0].identity_evidence) == len(subjects["IMPORTANT_ITEM"][0].identity_evidence) == 2


def test_category_scoped_split_preserves_hidden_provisional_targets_for_later_segments():
    candidates = [candidate(1), candidate(2, "탑 기사단", category="FACTION"), candidate(3)]
    outcomes, client, subjects = run(candidates, [
        truncated(), result(row("C1")), truncated(), result(row("C2")), result(row("C3", ["N1"])),
    ], subjects={"LOCATION": [], "FACTION": []})
    faction_request = json.loads(client.calls[-2]["user_prompt"])
    last_request = json.loads(client.calls[-1]["user_prompt"])
    assert faction_request["initial_subjects"] == []
    assert [item["ref"] for item in last_request["initial_subjects"]] == ["N1"]
    assert outcomes[0].provisional_subject_keys == outcomes[2].provisional_subject_keys
    assert outcomes[1].provisional_subject_keys == [f"provisional-world:{UUID(int=2)}"]
    assert subjects["LOCATION"][0].provisional_subject_key == f"provisional-world:{UUID(int=1)}"
    assert subjects["FACTION"][0].provisional_subject_key == f"provisional-world:{UUID(int=2)}"


def test_unasserted_book_candidates_cannot_create_or_select_anchors():
    outcomes, client, subjects = run([candidate(1, book=True, asserted=False)], [])
    assert outcomes[0].ambiguous and not outcomes[0].provisional_subject_keys
    assert client.calls == [] and subjects == {"LOCATION": []}


def test_book_proofs_can_use_only_earlier_successfully_enriched_target_evidence():
    bridge = "백탑은 흰 탑의 다른 이름이다."
    candidates = [candidate(1, book=True, quote="백탑이 우뚝 서 있다."),
                  candidate(2, book=True, quote=bridge), candidate(3, "흰 탑", book=True, quote="그곳은 흰 탑이다.")]
    proof = {"subject_ref": "N1", "relation": "SAME_ENTITY", "quote": bridge}
    outcomes, client, subjects = run(candidates, [result(
        row("C1"), row("C2", ["N1"]), row("C3", ["N1"], proofs=[proof]),
    )])
    assert len(client.calls) == 1
    assert len({tuple(item.provisional_subject_keys) for item in outcomes}) == 1
    assert len(subjects["LOCATION"][0].identity_evidence) == 3


def test_book_proof_cannot_borrow_future_candidate_evidence():
    bridge = "백탑은 흰 탑의 다른 이름이다."
    proof = {"subject_ref": "S1", "relation": "SAME_ENTITY", "quote": bridge}
    outcomes, client, _ = run([candidate(1, "흰 탑", book=True), candidate(2, book=True, quote=bridge)], [
        result(row("C1", ["S1"], proofs=[proof]), row("C2", ["S1"])),
        result(row("C1", ambiguous=True), row("C2", ["S1"])),
    ], subjects={"LOCATION": [target()]}, attempts=2)
    assert len(client.calls) == 2 and outcomes[0].ambiguous
    assert json.loads(client.calls[-1]["user_prompt"])["validation_feedback"]["reason_code"] == "BOOK_IDENTITY_PROOF_INVALID"


def truncated():
    return LlmOutputTruncatedError("synthetic", incomplete_reason="max_output_tokens", max_output_tokens=32000)


@pytest.mark.parametrize("failure", [truncated(), ComparisonValidationError("synthetic")])
def test_batch_failure_splits_in_order_and_later_request_receives_validated_state(failure):
    candidates = [candidate(1), candidate(2), candidate(3), candidate(4)]
    outcomes, client, subjects = run(candidates, [
        failure, result(row("C1"), row("C2", ["N1"])), result(row("C3", ["N1"]), row("C4", ["N1"])),
    ])
    assert len(client.calls) == 3
    assert len({tuple(item.provisional_subject_keys) for item in outcomes}) == 1
    later = json.loads(client.calls[-1]["user_prompt"])
    assert later["initial_subjects"][0]["ref"] == "N1"
    assert later["initial_subjects"][0]["identity_evidence"] == [
        candidates[0].evidence_spans[0].quote, candidates[1].evidence_spans[0].quote,
    ]
    assert len(subjects["LOCATION"][0].identity_evidence) == 4


def test_failed_singleton_has_typed_failure_and_never_becomes_a_later_target():
    candidates = [candidate(1), candidate(2, "흰 탑"), candidate(3)]
    outcomes, client, subjects = run(candidates, [
        truncated(), result(row("C1")), truncated(),
        result(row("C2", ["SECRET_UNKNOWN_TARGET"])), result(row("C3", ["N1"])),
    ])
    assert outcomes[1].failure_code == AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    assert not outcomes[1].ambiguous and not outcomes[1].provisional_subject_keys
    assert outcomes[0].provisional_subject_keys == outcomes[2].provisional_subject_keys
    later = json.loads(client.calls[-1]["user_prompt"])
    assert [item["ref"] for item in later["initial_subjects"]] == ["N1"]
    assert len(subjects["LOCATION"][0].identity_evidence) == 2


def test_oversized_input_splits_without_dropping_context_or_generating_oversized_request():
    candidates = [candidate(1, quote="a" * 3000), candidate(2, quote="b" * 3000)]
    # Measure the static instructions independently of the size threshold. The
    # confirmed-only mode preserves its initial state across the split.
    _, probe, _ = run([candidate(1)], [result(row("C1"))], subjects={"LOCATION": [target(name="붉은 탑")]}, ordered=False)
    base = probe.calls[0]
    from app.analysis.ordered_context import ensure_ordered_prompt_fits
    baseline = ensure_ordered_prompt_fits(base["system_prompt"], base["user_prompt"], response_schema=base["response_schema"])
    outcomes, client, _ = run(candidates, [result(row("C1")), result(row("C2"))],
        subjects={"LOCATION": [target(name="붉은 탑")]}, ordered=False, input_limit=baseline + 4000)
    assert len(client.calls) == 2 and len(outcomes) == 2
    assert all(item.failure_code is None for item in outcomes)


@pytest.mark.parametrize("make_error", [
    AiTokenQuotaExhaustedError,
    lambda: OrderedInputContextError("frozen source changed"),
    lambda: httpx.HTTPStatusError("unauthorized", request=httpx.Request("POST", "https://offline.invalid"),
                                 response=httpx.Response(401)),
    lambda: httpx.HTTPStatusError("quota", request=httpx.Request("POST", "https://offline.invalid"),
                                 response=httpx.Response(429, json={"error": {"code": "insufficient_quota"}})),
])
def test_execution_and_account_failures_propagate_without_split_or_partial_state(make_error):
    original = target()
    subjects = {"LOCATION": [original]}
    error = make_error()
    with pytest.raises(type(error)):
        run([candidate(1), candidate(2)], [error], subjects=subjects)
    assert subjects == {"LOCATION": [original]}


def test_pipeline_saves_one_complete_manuscript_resolution_before_comparison_claim():
    from app.analysis.world_setting_pipeline import WorldSettingComparisonPipeline
    from tests.test_automatic_subject_failures import WorldSpring

    spring = WorldSpring()
    client = Client([result(row("C1"), row("C2"), row("C3", ["N1"]))])
    resolver = WorldSettingSubjectResolver(llm_client=client, model="gpt-6-luna", max_attempts=1,
                                          max_output_tokens=2000)
    asyncio.run(WorldSettingComparisonPipeline(spring, resolver, object())._prepare_subject_resolutions(
        UUID(int=800), UUID(int=801), spring.context, continue_on_candidate_failure=True,
    ))
    assert len(client.calls) == len(spring.saved) == 1
    first, second, last = spring.saved[0].resolutions
    assert first.provisional_subject_keys == last.provisional_subject_keys
    assert first.provisional_subject_keys != second.provisional_subject_keys
    assert [item.candidate_id for item in spring.saved[0].resolutions] == [item.candidate_id for item in spring.sources]
