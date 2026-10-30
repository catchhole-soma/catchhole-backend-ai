import asyncio
import copy
import json

import pytest

from app.analysis.exceptions import ComparisonValidationError, OrderedInputContextError
from app.analysis.relationship_comparator import (
    RelationshipComparator,
    RelationshipComparisonResponse,
    relationship_comparison_prompt_data,
    validate_relationship_decisions,
)
from app.analysis.relationship_observations import RelationshipInputTooLargeError
from app.llm.responses import LlmTextResponse

QUOTE = "하린은 노아와 다시 힘을 합치기로 했다."


def observation(ref="O1", *, source="N1", target="N2", **extra):
    return {
        "ref": ref, "source": {"name": "하린", "mention": "하린"},
        "target": {"name": "노아", "mention": "노아"}, "summary": "두 사람이 다시 협력하기로 한다.",
        "sourceNodeRefs": [source], "targetNodeRefs": [target],
        "evidence": [{"quote": QUOTE, "startOffset": 5, "endOffset": 5 + len(QUOTE)}], **extra,
    }


def relationship(**updates):
    return {
        "ref": "R1", "sourceNodeRef": "N1", "targetNodeRef": "N2", "axis": "INTERACTION",
        "kind": "COOPERATION", "label": "협력", "summary": "두 사람이 협력하고 있다.",
        "direction": "MUTUAL", "status": "ACTIVE", "knownFromEpisodeNo": 2,
        "lastChangedEpisodeNo": 2, **updates,
    }


def context(**updates):
    return {
        "formatVersion": 1, "contextToken": "secret-token", "status": "PREPARED", "sourceEpisodeNo": 4,
        "observations": [observation()], "heldObservations": [],
        "nodes": [{"ref": "N1", "name": "하린", "profileRegistered": True, "id": "private-node-id"},
                  {"ref": "N2", "name": "노아", "profileRegistered": False}],
        "relationships": [], **updates,
    }


def decision(**updates):
    return {
        "observationRefs": ["O1"], "operation": "ADD", "relationshipRef": None,
        "sourceNodeRef": "N1", "targetNodeRef": "N2", "axis": "INTERACTION", "kind": "COOPERATION",
        "label": "협력", "summary": "두 사람이 협력하고 있다.", "direction": "MUTUAL",
        "temporalScope": "PRESENT", "reason": None, **updates,
    }


def hold(**updates):
    return decision(operation="HOLD", sourceNodeRef=None, targetNodeRef=None, axis=None, kind=None,
                    label=None, summary=None, direction=None, reason="누구를 말하는지 불명확하다.", **updates)


def validate(rows, data=None):
    return validate_relationship_decisions(
        RelationshipComparisonResponse.model_validate({"decisions": rows}),
        relationship_comparison_prompt_data(data or context()),
    )


class Client:
    def __init__(self, rows=None, raw=None, error=None):
        self.calls = []
        self.raw = json.dumps({"decisions": rows or []}, ensure_ascii=False) if raw is None else raw
        self.error = error

    async def create_text_response(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return LlmTextResponse(text=self.raw, input_token_count=1, output_token_count=2)


def compare(client, data=None, **kwargs):
    return asyncio.run(RelationshipComparator(client, **kwargs).compare(data or context()))


def test_one_frozen_terra_call_uses_local_refs_and_excludes_storage_and_credentials():
    client = Client([decision()])
    data = context(analysisJobId="private-job-id", sourceHash="private-hash")
    result = compare(client, data)
    assert result == [decision()]
    request, = client.calls
    assert request["model"] == "gpt-6.1-terra"
    assert request["call_options"].reasoning_effort == "medium"
    assert request["max_output_tokens"] == 16000
    prompt = request["user_prompt"]
    for private in ("secret-token", "private-node-id", "private-job-id", "private-hash", "contextToken"):
        assert private not in prompt
    assert json.loads(prompt)["nodes"][1]["profileRegistered"] is False
    assert data["nodes"][0]["id"] == "private-node-id"  # caller input is not mutated


def test_one_observation_can_end_friendship_and_add_hostility():
    friendship = relationship(axis="SOCIAL", kind="FRIENDSHIP", label="친구", summary="서로 친구다.")
    rows = [decision(operation="END", relationshipRef="R1", axis="SOCIAL", kind="FRIENDSHIP",
                     label="친구 관계 종료", summary="배신으로 친구 관계가 끝났다."),
            decision(axis="ATTITUDE", kind="HOSTILITY", label="적대", direction="DIRECTED",
                     summary="하린이 노아를 적대한다.")]
    assert validate(rows, context(relationships=[friendship])) == rows


def test_reciprocal_feelings_can_be_two_distinct_one_sided_edges():
    rows = [decision(axis="ATTITUDE", kind="TRUST", direction="DIRECTED", label="신뢰"),
            decision(axis="ATTITUDE", kind="DISTRUST", direction="DIRECTED", label="불신",
                     sourceNodeRef="N2", targetNodeRef="N1")]
    assert validate(rows) == rows


def test_grouped_observations_cover_inputs_without_requiring_unmentioned_edge_changes():
    obs = [observation(), observation("O2")]
    unrelated = relationship(ref="R2", kind="HOSTILITY", direction="DIRECTED")
    row = decision(observationRefs=["O1", "O2"], operation="MERGE", relationshipRef="R1")
    assert validate([row], context(observations=obs, relationships=[relationship(), unrelated])) == [row]


@pytest.mark.parametrize("operation", ["MERGE", "UPDATE", "END"])
def test_existing_reference_must_match_pair_axis_kind_and_direction(operation):
    for changes in ({"kind": "TRUST"}, {"axis": "ATTITUDE"}, {"direction": "DIRECTED"},
                    {"relationshipRef": "R999"}):
        with pytest.raises(ValueError):
            validate([decision(operation=operation, relationshipRef="R1", **changes)]
                     if "relationshipRef" not in changes else [decision(operation=operation, **changes)],
                     context(relationships=[relationship()]))


def test_ended_relation_requires_update_to_reactivate_not_add_or_merge():
    data = context(relationships=[relationship(status="ENDED")])
    assert validate([decision(operation="UPDATE", relationshipRef="R1")], data)[0]["operation"] == "UPDATE"
    for row in [decision(), decision(operation="MERGE", relationshipRef="R1"),
                decision(operation="END", relationshipRef="R1")]:
        with pytest.raises(ValueError):
            validate([row], data)


def test_merge_cannot_rewrite_current_summary():
    with pytest.raises(ValueError, match="preserve"):
        validate([decision(operation="MERGE", relationshipRef="R1", summary="새로운 다른 관계")],
                 context(relationships=[relationship()]))


@pytest.mark.parametrize("temporal", ["PAST", "HYPOTHETICAL", "UNKNOWN"])
@pytest.mark.parametrize("operation", ["ADD", "UPDATE", "MERGE", "END"])
def test_nonpresent_cannot_mutate_current_graph(temporal, operation):
    with pytest.raises(ValueError, match="Non-present"):
        validate([decision(operation=operation, temporalScope=temporal)], context())
    assert validate([hold(temporalScope=temporal)])[0]["operation"] == "HOLD"


@pytest.mark.parametrize("source_refs,target_refs", [([], ["N2"]), (["N1", "N2"], ["N2"]),
                                                   (["N1"], ["N1"]), (["N2"], ["N2"])])
def test_ambiguous_or_same_endpoints_are_held_without_user_review(source_refs, target_refs):
    item = observation(sourceNodeRefs=source_refs, targetNodeRefs=target_refs)
    data = context(observations=[item])
    with pytest.raises(ValueError, match="endpoints"):
        validate([decision()], data)
    assert validate([hold()], data) == [hold()]


def test_node_from_another_observation_cannot_be_used_to_complete_this_pair():
    data = context()
    data["nodes"].append({"ref": "N3", "name": "다른 사람", "profileRegistered": True})
    with pytest.raises(ValueError, match="endpoints"):
        validate([decision(targetNodeRef="N3")], data)


@pytest.mark.parametrize("rows", [[], [decision(observationRefs=["O2"])],
                                   [decision(observationRefs=["O1", "O1"])],
                                   [hold(), decision()], [decision(), hold()], [hold(), hold()],
                                   [decision(), decision(sourceNodeRef="N2", targetNodeRef="N1")]])
def test_missing_duplicate_or_contradictory_coverage_fails(rows):
    with pytest.raises(ValueError):
        validate(rows)


def test_old_hold_is_only_reconsidered_with_new_evidence_and_keeps_original_episode():
    data = context(heldObservations=[observation("H1", sourceEpisodeNo=1)])
    payload = relationship_comparison_prompt_data(data)
    assert payload["heldObservations"][0]["sourceEpisodeNo"] == 1
    assert validate([decision(observationRefs=["O1", "H1"])], data)
    with pytest.raises(ValueError):
        validate([decision(), decision(observationRefs=["H1"], kind="TRUST", direction="DIRECTED")], data)


@pytest.mark.parametrize("update", [{"knownFromEpisodeNo": 5}, {"lastChangedEpisodeNo": 5},
                                    {"knownFromEpisodeNo": 3, "lastChangedEpisodeNo": 2}])
def test_future_relationship_context_is_rejected_before_provider(update):
    client = Client([decision()])
    with pytest.raises(OrderedInputContextError, match="context"):
        compare(client, context(relationships=[relationship(**update)]))
    assert client.calls == []


@pytest.mark.parametrize("path,value", [("sourceEpisodeNo", 0), ("sourceEpisodeNo", True),
                                       ("formatVersion", True), ("nodes", []),
                                       ("observations", "not an array")])
def test_invalid_input_fails_before_provider(path, value):
    client = Client([decision()])
    with pytest.raises(ComparisonValidationError):
        compare(client, context(**{path: value}))
    assert client.calls == []


def test_invalid_evidence_offsets_and_future_held_observations_fail_before_provider():
    data = context()
    data["observations"][0]["evidence"][0]["endOffset"] += 1
    with pytest.raises(ComparisonValidationError):
        relationship_comparison_prompt_data(data)
    with pytest.raises(ComparisonValidationError):
        relationship_comparison_prompt_data(context(heldObservations=[observation("H1", sourceEpisodeNo=5)]))


@pytest.mark.parametrize("raw", ['{"decisions":[] ,"decisions":[]}', "null", "not json",
                                  '{"decisions":[],"secret":"provider manuscript"}'])
def test_invalid_response_is_safe_single_attempt(raw):
    client = Client(raw=raw)
    with pytest.raises(ComparisonValidationError, match="^Invalid relationship comparison response.$"):
        compare(client)
    assert len(client.calls) == 1


@pytest.mark.parametrize("field", ["summary", "label", "reason"])
@pytest.mark.parametrize("text", ["O1에서 확인됨", "00000000-0000-0000-0000-000000000001"])
def test_display_text_does_not_expose_internal_refs(field, text):
    with pytest.raises(ValueError, match="internal"):
        validate([decision(**{field: text})])


def test_empty_input_skips_call_and_oversized_input_fails_without_trimming():
    client = Client([decision()])
    assert compare(client, context(observations=[])) == []
    assert client.calls == []
    with pytest.raises(RelationshipInputTooLargeError):
        compare(client, max_input_tokens=1)
    assert client.calls == []


def test_provider_failure_propagates_once_without_repair_calls():
    client = Client(error=TimeoutError("synthetic"))
    with pytest.raises(TimeoutError):
        compare(client)
    assert len(client.calls) == 1


def test_validation_does_not_mutate_frozen_context_or_model_decisions():
    data, rows = context(), [decision()]
    before = copy.deepcopy((data, rows))
    validate(rows, data)
    assert (data, rows) == before


@pytest.mark.parametrize("patch", [{"sourceNodeRefs": [{}]}, {"targetNodeRefs": [None]}])
def test_malformed_unhashable_backend_references_are_safe_input_errors(patch):
    client = Client([decision()])
    with pytest.raises(OrderedInputContextError, match="^Invalid frozen relationship context.$"):
        compare(client, context(observations=[observation(**patch)]))
    assert client.calls == []
