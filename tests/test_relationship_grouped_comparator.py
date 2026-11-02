"""Synthetic responses exercise contracts, not model semantic accuracy."""

import asyncio
import copy
import json

import httpx
import pytest

from app.analysis.exceptions import OrderedInputContextError
from app.analysis.relationship_comparator import RelationshipComparator
from app.analysis.relationship_grouped_comparator import GroupedRelationshipComparator
from app.clients.exceptions import (
    AiTokenQuotaExhaustedError,
    SpringWorkerTransportError,
    WorkerLeaseExpiredError,
)
from app.llm.exceptions import LlmOutputTruncatedError
from app.llm.responses import LlmTextResponse
from tests.test_relationship_comparator import context, observation, relationship


def data(**updates):
    return context(**{"supportsPartialResults": True, "blockedPairs": [], **updates})


def disjoint(count=2):
    return data(
        nodes=[{"ref": f"N{i}", "name": f"인물{i}", "profileRegistered": False} for i in range(1, count * 2 + 1)],
        observations=[observation(f"O{i}", source=f"N{i * 2 - 1}", target=f"N{i * 2}") for i in range(1, count + 1)],
    )


def decision(**updates):
    return {
        "observationRefs": ["O1"], "operation": "ADD", "relationshipRef": None,
        "axis": "INTERACTION", "kind": "COOPERATION", "label": "협력", "summary": "두 사람이 협력한다.",
        "direction": "MUTUAL", "temporalScope": "PRESENT", "reason": None, **updates,
    }


def existing(ref="R1", **updates):
    return decision(**{"operation": "MERGE", "relationshipRef": ref, "axis": None, "kind": None,
                       "label": None, "summary": None, "direction": None, **updates})


def output(request):
    return {"groups": [{"groupRef": group["groupRef"], "decisions": [
        decision(observationRefs=[row["ref"] for row in group["observations"]])
    ]} for group in request["groups"]]}


class Client:
    def __init__(self, transform=None, error=None):
        self.calls, self.transform, self.error = [], transform, error

    async def create_text_response(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        request = json.loads(kwargs["user_prompt"])
        result = output(request)
        if self.transform:
            result = self.transform(request, result, len(self.calls))
        return LlmTextResponse(text=result if isinstance(result, str) else json.dumps(result, ensure_ascii=False))


def run(client, value=None, **kwargs):
    return asyncio.run(RelationshipComparator(client, **kwargs).compare_partial(value or data()))


def test_always_scopes_groups_and_removes_unrelated_context_even_below_budget():
    value = disjoint()
    value["nodes"] += [{"ref": "N5", "name": "무관한 인물", "profileRegistered": False}]
    value["relationships"] = [relationship(sourceNodeRef="N1", targetNodeRef="N5")]
    value["heldObservations"] = [observation("H1", source="N1", target="N5", sourceEpisodeNo=1),
                                 observation("H2", source="N1", target="N2", sourceEpisodeNo=1)]
    before = copy.deepcopy(value)
    client = Client()
    result = run(client, value)
    request = json.loads(client.calls[0]["user_prompt"])
    assert len(client.calls) == 1 and len(request["groups"]) == 2
    assert [row["ref"] for row in request["groups"][0]["nodes"]] == ["N1", "N2"]
    assert [row["ref"] for row in request["groups"][0]["heldObservations"]] == ["H2"]
    assert request["groups"][0]["relationships"] == []
    assert result.failures == [] and len(result.decisions) == 2
    assert value == before
    assert "contextToken" not in client.calls[0]["user_prompt"]


@pytest.mark.parametrize("source,target", [([], ["N2"]), (["N1", "N2"], ["N2"]), (["N1"], ["N1"])])
def test_unresolved_new_observations_are_internal_hold_without_provider(source, target):
    client = Client()
    result = run(client, data(observations=[observation(sourceNodeRefs=source, targetNodeRefs=target)]))
    assert not client.calls and not result.failures
    assert result.decisions[0]["operation"] == "HOLD"
    assert result.decisions[0]["sourceNodeRef"] is None


def test_blocked_pair_is_explicit_incomplete_while_independent_pair_progresses():
    value = disjoint()
    value["blockedPairs"] = [{"sourceNodeRef": "N2", "targetNodeRef": "N1", "firstFailedEpisodeNo": 2}]
    client = Client()
    result = run(client, value)
    assert result.failures == [{"observationRefs": ["O1"], "reasonCode": "RELATIONSHIP_PREDECESSOR_INCOMPLETE"}]
    assert result.decisions[0]["observationRefs"] == ["O2"]
    assert [g["groupRef"] for g in json.loads(client.calls[0]["user_prompt"])["groups"]] == ["G2"]


@pytest.mark.parametrize("field,value", [("firstFailedEpisodeNo", 4), ("sourceNodeRef", "N999"), ("firstFailedEpisodeNo", True)])
def test_bad_blocked_pair_is_hard_input_error(field, value):
    row = {"sourceNodeRef": "N1", "targetNodeRef": "N2", "firstFailedEpisodeNo": 2, field: value}
    client = Client()
    with pytest.raises(OrderedInputContextError):
        run(client, data(blockedPairs=[row]))
    assert not client.calls


@pytest.mark.parametrize("direction,source,target,expected", [
    ("LEFT_TO_RIGHT", "N1", "N2", "DIRECTED"), ("RIGHT_TO_LEFT", "N2", "N1", "DIRECTED"),
    ("MUTUAL", "N1", "N2", "MUTUAL"), ("UNDIRECTED", "N1", "N2", "UNDIRECTED"),
])
def test_add_direction_uses_only_fixed_pair(direction, source, target, expected):
    client = Client(lambda req, out, call: {"groups": [{"groupRef": "G1", "decisions": [decision(direction=direction)]}]})
    row, = run(client).decisions
    assert (row["sourceNodeRef"], row["targetNodeRef"], row["direction"]) == (source, target, expected)


def test_existing_ref_restores_owned_fields_and_merge_content():
    old = relationship(sourceNodeRef="N2", targetNodeRef="N1", direction="DIRECTED")
    client = Client(lambda req, out, call: {"groups": [{"groupRef": "G1", "decisions": [existing()]}]})
    row, = run(client, data(relationships=[old])).decisions
    for field in ("sourceNodeRef", "targetNodeRef", "axis", "kind", "direction", "label", "summary"):
        assert row[field] == old[field]


def test_invalid_group_repairs_once_without_repeating_valid_group():
    def respond(req, out, call):
        if call == 1:
            out["groups"][0]["decisions"][0]["observationRefs"] = ["O2"]
        return out
    client = Client(respond)
    result = run(client, disjoint())
    assert len(client.calls) == 2 and not result.failures
    repair = json.loads(client.calls[1]["user_prompt"])["groups"]
    assert [g["groupRef"] for g in repair] == ["G1"]
    assert repair[0]["validationFeedback"] == "RELATIONSHIP_OBSERVATION_REFS_INVALID"
    assert [row["observationRefs"] for row in result.decisions] == [["O1"], ["O2"]]


def test_repeated_cross_group_failure_preserves_normal_group_as_typed_partial():
    def respond(req, out, call):
        out["groups"][0]["decisions"][0]["observationRefs"] = ["O2"]
        return out
    client = Client(respond)
    result = run(client, disjoint())
    assert len(client.calls) == 2
    assert result.failures == [{"observationRefs": ["O1"], "reasonCode": "RELATIONSHIP_OBSERVATION_REFS_INVALID"}]
    assert [row["observationRefs"] for row in result.decisions] == [["O2"]]
    assert all(row["operation"] != "HOLD" for row in result.decisions)


@pytest.mark.parametrize("mode", ["missing", "duplicate", "invalid_schema"])
def test_group_envelope_failures_only_repair_affected_group(mode):
    def respond(req, out, call):
        if call != 1:
            return out
        if mode == "missing":
            out["groups"].pop(0)
        elif mode == "duplicate":
            out["groups"].append(copy.deepcopy(out["groups"][0]))
        else:
            out["groups"][0]["decisions"][0]["sourceNodeRef"] = "private response value"
        return out
    client = Client(respond)
    assert not run(client, disjoint()).failures
    assert [g["groupRef"] for g in json.loads(client.calls[1]["user_prompt"])["groups"]] == ["G1"]
    assert "private response value" not in client.calls[1]["user_prompt"]


@pytest.mark.parametrize("raw", ["private malformed response", '{"groups":[],"groups":[]}', '{"groups":[{"groupRef":"G999"}]}'])
def test_unassignable_response_repairs_whole_batch_once_then_records_failure(raw):
    client = Client(lambda *_: raw)
    result = run(client, disjoint())
    assert len(client.calls) == 2 and len(result.failures) == 2 and result.decisions == []
    assert "private malformed response" not in json.dumps(result.failures)


def test_one_observation_can_end_and_add_distinct_slots():
    old = relationship(axis="SOCIAL", kind="FRIENDSHIP")
    rows = [existing(operation="END", label="친분 종료", summary="두 사람의 친분이 끝났다."),
            decision(axis="ATTITUDE", kind="HOSTILITY", direction="LEFT_TO_RIGHT")]
    client = Client(lambda *_: {"groups": [{"groupRef": "G1", "decisions": rows}]})
    result = run(client, data(relationships=[old]))
    assert not result.failures and len(result.decisions) == 2


@pytest.mark.parametrize("change,reason", [
    ({"temporalScope": "PAST"}, "RELATIONSHIP_NONPRESENT_MUTATION"),
    ({"summary": "G1의 관계"}, "RELATIONSHIP_DISPLAY_REFERENCE_LEAK"),
    ({"observationRefs": ["O1", "O1"]}, "RELATIONSHIP_OBSERVATION_REFS_INVALID"),
])
def test_strict_existing_business_invariants_remain(change, reason):
    client = Client(lambda *_: {"groups": [{"groupRef": "G1", "decisions": [decision(**change)]}]})
    result = run(client)
    assert result.failures[0]["reasonCode"] == reason and not result.decisions


@pytest.mark.parametrize("error", [
    AiTokenQuotaExhaustedError(), SpringWorkerTransportError("private"),
    WorkerLeaseExpiredError("private", request=httpx.Request("POST", "http://test"), response=httpx.Response(409)),
    httpx.ReadTimeout("private"), RuntimeError("private"),
    LlmOutputTruncatedError("private", incomplete_reason="max_output_tokens", max_output_tokens=16000),
])
def test_provider_execution_errors_propagate_without_repair_or_partial(error):
    client = Client(error=error)
    with pytest.raises(type(error)):
        run(client, disjoint())
    assert len(client.calls) == 1


def test_preflight_budget_is_bounded_and_preserves_oversized_pairs_as_failures(monkeypatch):
    monkeypatch.setattr(GroupedRelationshipComparator, "_fits", lambda self, groups: len(groups) <= 1)
    client = Client(lambda req, out, call: {"groups": []})
    result = run(client, disjoint(17))
    assert len(client.calls) == 32  # 16 initial + one repair each; never retry the 17th.
    assert len(result.failures) == 17
    assert result.failures[-1] == {"observationRefs": ["O17"], "reasonCode": "RELATIONSHIP_INPUT_TOO_LARGE"}
    assert len({ref for row in result.failures for ref in row["observationRefs"]}) == 17


def test_single_oversized_group_does_not_prevent_other_group(monkeypatch):
    monkeypatch.setattr(GroupedRelationshipComparator, "_fits", lambda self, groups: all(g["groupRef"] != "G1" for g in groups))
    client = Client()
    result = run(client, disjoint())
    assert len(client.calls) == 1
    assert result.failures == [{"observationRefs": ["O1"], "reasonCode": "RELATIONSHIP_INPUT_TOO_LARGE"}]
    assert result.decisions[0]["observationRefs"] == ["O2"]


def test_successful_local_groups_cannot_bypass_global_512_decision_limit(monkeypatch):
    import itertools
    from typing import get_args

    from app.analysis.relationship_comparator import Axis, Kind

    def respond(req, out, call):
        for group, result in zip(req["groups"], out["groups"]):
            result["decisions"] = [decision(
                observationRefs=[group["observations"][0]["ref"]], axis=axis, kind=kind, direction=direction,
            ) for axis, kind, direction in itertools.islice(
                itertools.product(get_args(Axis), get_args(Kind), ["MUTUAL", "UNDIRECTED", "LEFT_TO_RIGHT", "RIGHT_TO_LEFT"]), 40,
            )]
        return out
    monkeypatch.setattr(GroupedRelationshipComparator, "_fits", lambda *_: True)
    client = Client(respond)
    result = run(client, disjoint(13))
    assert len(client.calls) == 1 and len(result.decisions) == 480
    assert result.failures == [{"observationRefs": ["O13"], "reasonCode": "RELATIONSHIP_RESPONSE_SCHEMA_INVALID"}]
