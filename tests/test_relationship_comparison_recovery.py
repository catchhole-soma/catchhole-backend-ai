"""Synthetic bounded recovery: no provider requests, credentials or database."""

import asyncio
import copy
import itertools
import json

import httpx
import pytest

from app.analysis.exceptions import ComparisonValidationError, OrderedAnalysisIncompleteError
from app.analysis.relationship_comparator import (
    RelationshipComparator,
    relationship_comparison_prompt_data,
)
from app.analysis.relationship_observations import RelationshipInputTooLargeError
from app.domain.enums import AnalysisFailureCode
from app.exceptions.failure_classification import analysis_failure_code, failure_diagnostics
from app.llm.responses import LlmTextResponse
from app.storage.s3 import SourceObjectTooLargeError
from tests.test_relationship_comparator import (
    Client,
    context,
    decision,
    hold,
    observation,
    relationship,
)
from tests.test_relationship_only_worker import setup_worker


def disjoint_context(count=3):
    return context(
        observations=[observation(f"O{i + 1}", source=f"N{2 * i + 1}", target=f"N{2 * i + 2}")
                      for i in range(count)],
        nodes=[{"ref": f"N{i + 1}", "name": f"합성 인물 {i + 1}", "profileRegistered": True}
               for i in range(2 * count)],
    )


def observation_budget(monkeypatch):
    # Separate the partition boundary from tokenizer implementation, including
    # the prompt/schema constant shared by every production request.
    monkeypatch.setattr(
        "app.analysis.relationship_comparator.estimate_text_input_tokens",
        lambda system, user, model, schema: 100 * len(json.loads(user)["observations"]),
    )


class PerPairClient:
    def __init__(self, *, fail_call=None):
        self.calls = []
        self.fail_call = fail_call
        self.active = 0
        self.maximum_active = 0

    async def create_text_response(self, **kwargs):
        self.active += 1
        self.maximum_active = max(self.maximum_active, self.active)
        self.calls.append(kwargs)
        await asyncio.sleep(0)
        data = json.loads(kwargs["user_prompt"])
        groups, rows = {}, []
        for item in data["observations"]:
            source, target = item["sourceNodeRefs"], item["targetNodeRefs"]
            if len(source) != 1 or len(target) != 1 or source == target:
                rows.append(hold(observationRefs=[item["ref"]]))
            else:
                groups.setdefault(tuple(sorted((source[0], target[0]))), []).append(item["ref"])
        for pair, refs in groups.items():
            rows.append(decision(observationRefs=refs, sourceNodeRef=pair[0], targetNodeRef=pair[1]))
        self.active -= 1
        if self.fail_call == len(self.calls):
            rows = []
        return LlmTextResponse(text=json.dumps({"decisions": rows}), input_token_count=1, output_token_count=2)


def run(client, data, budget):
    return asyncio.run(RelationshipComparator(client, max_input_tokens=budget).compare(data))


def test_fitting_context_retains_exact_full_prompt_and_single_call(monkeypatch):
    observation_budget(monkeypatch)
    data = context(heldObservations=[observation("H1", sourceEpisodeNo=1)])
    client = Client([decision()])
    assert run(client, data, 100) == [decision()]
    assert len(client.calls) == 1
    assert client.calls[0]["user_prompt"] == json.dumps(relationship_comparison_prompt_data(data), ensure_ascii=False)


def test_oversized_context_partitions_without_renumbering_and_runs_sequentially(monkeypatch):
    observation_budget(monkeypatch)
    data = disjoint_context()
    data["heldObservations"] = [observation("H7", source="N3", target="N4", sourceEpisodeNo=1)]
    before = copy.deepcopy(data)
    client = PerPairClient()
    rows = run(client, data, 100)
    assert [row["observationRefs"] for row in rows] == [["O1"], ["O2"], ["O3"]]
    payloads = [json.loads(call["user_prompt"]) for call in client.calls]
    assert [[row["ref"] for row in part["nodes"]] for part in payloads] == [
        ["N1", "N2"], ["N3", "N4"], ["N5", "N6"],
    ]
    assert [[row["ref"] for row in part["heldObservations"]] for part in payloads] == [[], ["H7"], []]
    assert client.maximum_active == 1
    assert data == before
    assert all(call["model"] == "gpt-6.1-terra" and call["call_options"].reasoning_effort == "medium"
               and call["max_output_tokens"] == 16000 for call in client.calls)


def test_same_pair_and_reverse_direction_observations_are_never_split(monkeypatch):
    observation_budget(monkeypatch)
    data = disjoint_context()
    data["observations"][1] = observation("O2", source="N2", target="N1")
    client = PerPairClient()
    rows = run(client, data, 200)
    assert [row["observationRefs"] for row in rows] == [["O1", "O2"], ["O3"]]
    assert len(client.calls) == 2


def test_ambiguous_observation_merges_every_potentially_overlapping_pair(monkeypatch):
    observation_budget(monkeypatch)
    data = disjoint_context(4)
    data["observations"][2] = observation("O3", sourceNodeRefs=["N1", "N3"], targetNodeRefs=["N2", "N4"])
    client = PerPairClient()
    run(client, data, 300)
    parts = [json.loads(call["user_prompt"]) for call in client.calls]
    assert [[row["ref"] for row in part["observations"]] for part in parts] == [
        ["O1", "O2", "O3"], ["O4"],
    ]


def test_transitive_held_bridges_merge_components_and_unrelated_holds_stay_untouched(monkeypatch):
    observation_budget(monkeypatch)
    data = disjoint_context(4)
    data["observations"] = [data["observations"][0], data["observations"][1], data["observations"][3]]
    data["heldObservations"] = [
        observation("H1", sourceNodeRefs=["N1", "N5"], targetNodeRefs=["N2", "N6"], sourceEpisodeNo=1),
        observation("H2", sourceNodeRefs=["N3", "N5"], targetNodeRefs=["N4", "N6"], sourceEpisodeNo=1),
        observation("H3", source="N1", target="N8", sourceEpisodeNo=1),
    ]
    before = copy.deepcopy(data)
    client = PerPairClient()
    run(client, data, 200)
    parts = [json.loads(call["user_prompt"]) for call in client.calls]
    assert [[row["ref"] for row in part["observations"]] for part in parts] == [["O1", "O2"], ["O4"]]
    assert [[row["ref"] for row in part["heldObservations"]] for part in parts] == [["H1", "H2"], []]
    assert data == before  # H3 is not excluded, consumed or removed from the frozen context.


def test_projection_preserves_all_existing_slots_for_the_pair(monkeypatch):
    observation_budget(monkeypatch)
    data = disjoint_context()
    data["relationships"] = [
        relationship(kind="FRIENDSHIP", axis="SOCIAL"),
        relationship(ref="R9", kind="HOSTILITY", direction="DIRECTED", sourceNodeRef="N2", targetNodeRef="N1"),
        relationship(ref="R12", sourceNodeRef="N3", targetNodeRef="N4", kind="TRUST"),
    ]
    client = PerPairClient()
    run(client, data, 100)
    parts = [json.loads(call["user_prompt"]) for call in client.calls]
    assert [[row["ref"] for row in part["relationships"]] for part in parts] == [["R1", "R9"], ["R12"], []]


def test_unresolved_endpoints_are_not_inferred_or_connected(monkeypatch):
    observation_budget(monkeypatch)
    data = disjoint_context()
    data["observations"][1]["sourceNodeRefs"] = []
    client = PerPairClient()
    rows = run(client, data, 100)
    assert rows[1]["operation"] == "HOLD"
    assert rows[1]["sourceNodeRef"] is None
    part = json.loads(client.calls[1]["user_prompt"])
    assert part["observations"][0]["sourceNodeRefs"] == []
    assert [row["ref"] for row in part["nodes"]] == ["N4"]


def test_all_partitions_are_checked_before_any_paid_call(monkeypatch):
    observation_budget(monkeypatch)
    data = disjoint_context()
    data["observations"][2] = observation("O3", source="N3", target="N4")
    client = PerPairClient()
    with pytest.raises(RelationshipInputTooLargeError) as error:
        run(client, data, 100)
    assert client.calls == []
    assert failure_diagnostics(error.value, stage="EXTRACTION").reason_code == "RELATIONSHIP_INPUT_TOO_LARGE"


def test_partition_count_limit_is_checked_before_any_paid_call(monkeypatch):
    observation_budget(monkeypatch)
    client = PerPairClient()
    with pytest.raises(RelationshipInputTooLargeError):
        run(client, disjoint_context(17), 100)
    assert client.calls == []


def test_partition_failure_does_not_attempt_output_repair_or_return_partial_decisions(monkeypatch):
    observation_budget(monkeypatch)
    client = PerPairClient(fail_call=2)
    with pytest.raises(ComparisonValidationError) as error:
        run(client, disjoint_context(), 100)
    assert len(client.calls) == 2
    diagnostic = failure_diagnostics(error.value, stage="EXTRACTION")
    assert diagnostic.reason_code == "RELATIONSHIP_OBSERVATION_COVERAGE_MISSING"
    assert diagnostic.stage == "RELATIONSHIP_COMPARE"


def test_global_response_schema_limit_is_not_bypassed_by_partitioning(monkeypatch):
    observation_budget(monkeypatch)

    class ManyKindsClient(PerPairClient):
        async def create_text_response(self, **kwargs):
            self.calls.append(kwargs)
            item, = json.loads(kwargs["user_prompt"])["observations"]
            combinations = itertools.product(
                ("SOCIAL", "ATTITUDE", "INTERACTION"),
                ("FRIENDSHIP", "COMPANIONSHIP", "COOPERATION", "MENTORSHIP", "ROMANCE", "HOSTILITY", "TRUST", "DISTRUST", "AFFECTION", "ACQUAINTANCE", "OTHER"),
                ("DIRECTED", "MUTUAL", "UNDIRECTED"),
            )
            rows = [decision(observationRefs=[item["ref"]], sourceNodeRef=item["sourceNodeRefs"][0],
                             targetNodeRef=item["targetNodeRefs"][0], axis=axis, kind=kind, direction=direction)
                    for axis, kind, direction in itertools.islice(combinations, 40)]
            return LlmTextResponse(text=json.dumps({"decisions": rows}))

    client = ManyKindsClient()
    with pytest.raises(ComparisonValidationError) as error:
        run(client, disjoint_context(13), 100)
    assert len(client.calls) == 13
    assert failure_diagnostics(error.value, stage="EXTRACTION").reason_code == "RELATIONSHIP_RESPONSE_SCHEMA_INVALID"


@pytest.mark.parametrize("rows,reason", [
    ([], "OBSERVATION_COVERAGE_MISSING"),
    ([decision(observationRefs=["O9"])], "OBSERVATION_REFS_INVALID"),
    ([hold(), decision()], "DISPOSITION_CONFLICT"),
    ([decision(summary="O1의 비공개 원문")], "DISPLAY_REFERENCE_LEAK"),
    ([decision(), decision()], "DUPLICATE_SLOT"),
])
def test_fixed_reason_codes_preserve_no_model_output(rows, reason):
    with pytest.raises(ComparisonValidationError) as error:
        run(Client(rows), context(), 64000)
    diagnostic = failure_diagnostics(error.value, stage="EXTRACTION")
    assert diagnostic.reason_code == "RELATIONSHIP_" + reason
    assert diagnostic.stage == "RELATIONSHIP_COMPARE"
    assert str(error.value) == "Invalid relationship comparison response."
    assert "비공개" not in diagnostic.model_dump_json()


@pytest.mark.parametrize("raw,reason", [
    ("private invalid JSON", "RELATIONSHIP_RESPONSE_JSON_INVALID"),
    ('{"decisions":null}', "RELATIONSHIP_RESPONSE_SCHEMA_INVALID"),
])
def test_response_parse_and_schema_reasons_are_distinct(raw, reason):
    with pytest.raises(ComparisonValidationError) as error:
        run(Client(raw=raw), context(), 64000)
    assert failure_diagnostics(error.value, stage="EXTRACTION").reason_code == reason


def test_dedicated_worker_preserves_comparison_rule_and_safe_message(monkeypatch):
    worker, payload, spring, _, _, comparator, _ = setup_worker(monkeypatch, status="PREPARED", observations=[{"ref": "O1"}])
    try:
        run(Client([]), context(), 64000)
    except ComparisonValidationError as cause:
        comparator.compare.side_effect = cause
    with pytest.raises(OrderedAnalysisIncompleteError) as error:
        asyncio.run(worker.process_claimed(payload))
    assert analysis_failure_code(error.value) == AnalysisFailureCode.COMPARISON_VALIDATION_FAILED
    report = spring.fail_relationship_job.call_args.kwargs
    assert report["failure_diagnostics"].reason_code == "RELATIONSHIP_OBSERVATION_COVERAGE_MISSING"
    assert report["failure_diagnostics"].stage == "RELATIONSHIP_COMPARE"
    assert spring.fail_relationship_analysis.call_args.args[2]["failureCode"] == "COMPARISON_FAILED"
    spring.complete_relationship_job.assert_not_called()


def test_dedicated_worker_keeps_provider_failure_category_without_leaking_provider_message(monkeypatch):
    worker, payload, spring, _, _, comparator, _ = setup_worker(monkeypatch, status="PREPARED", observations=[{"ref": "O1"}])
    comparator.compare.side_effect = httpx.ReadTimeout("private manuscript and private provider URL")
    with pytest.raises(OrderedAnalysisIncompleteError) as error:
        asyncio.run(worker.process_claimed(payload))
    assert "private" not in str(error.value)
    assert analysis_failure_code(error.value) == AnalysisFailureCode.LLM_NETWORK_ERROR
    report = spring.fail_relationship_job.call_args.kwargs
    assert report["failure_code"] == AnalysisFailureCode.LLM_NETWORK_ERROR
    assert report["failure_diagnostics"].reason_code == "LLM_TIMEOUT"
    assert report["failure_diagnostics"].stage == "RELATIONSHIP_COMPARE"
    assert "private" not in report["failure_diagnostics"].model_dump_json()


def test_input_limit_diagnostic_survives_dedicated_worker_failure(monkeypatch):
    worker, payload, spring, storage, _, _, _ = setup_worker(monkeypatch)
    storage.get_text.side_effect = SourceObjectTooLargeError("private source path")
    with pytest.raises(OrderedAnalysisIncompleteError):
        asyncio.run(worker.process_claimed(payload))
    diagnostic = spring.fail_relationship_job.call_args.kwargs["failure_diagnostics"]
    assert diagnostic.reason_code == "RELATIONSHIP_INPUT_TOO_LARGE"
    assert diagnostic.stage == "RELATIONSHIP_EXTRACTION"
