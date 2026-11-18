"""The same two changes in B/C, compared with the retained base behavior."""

import json
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.relationship_semantics_experiment.evaluate import evaluate_results
from scripts.relationship_semantics_experiment.input import Case, Query, load_document
from scripts.relationship_semantics_experiment.rdf_model import RdfModel
from scripts.relationship_semantics_experiment.typed_model import TypedModel

FIXTURE = Path(__file__).parent / "fixtures/relationship_semantics_extension_cases.json"
FACTORIES = (TypedModel, RdfModel)


@pytest.mark.parametrize("factory", FACTORIES)
def test_same_extension_examples_compute_all_seven_answers(factory):
    raw = json.loads(FIXTURE.read_text(encoding="utf-8"))
    results = []
    for case in load_document(raw).cases:
        model = factory(case, extended=True)
        assert model.validate()["conforms"]
        assert model.to_case().to_dict() == case.to_dict()
        results.extend(model.query(query) for query in case.queries)
    report = evaluate_results(raw, results)
    assert report["queryCount"] == 7
    assert report["passed"], report


@pytest.mark.parametrize("factory", FACTORIES)
def test_base_rejects_missing_vocabulary_and_reports_filter_as_unsupported(factory):
    cases = load_document(json.loads(FIXTURE.read_text(encoding="utf-8"))).cases
    assert not factory(cases[0]).validate()["conforms"]
    base = factory(cases[1])
    assert base.validate()["conforms"]
    result = base.query(cases[1].queries[1])
    assert result["status"] == "UNSUPPORTED"
    assert result["reason"] == "QUERY_FILTER_NOT_SUPPORTED"
    assert not result["facts"]


@pytest.mark.parametrize("factory", FACTORIES)
def test_new_kind_does_not_hardcode_axis_or_direction(factory):
    case = load_document(json.loads(FIXTURE.read_text(encoding="utf-8"))).cases[0].to_dict()
    case["facts"][0]["slot"].update(axis="SOCIAL", direction="MUTUAL")
    model = factory(Case.model_validate(case), extended=True)
    assert model.validate()["conforms"]
    result = model.query(Case.model_validate(case).queries[0])
    assert result["facts"][0]["slot"]["axis"] == "SOCIAL"
    assert result["facts"][0]["slot"]["direction"] == "MUTUAL"


@pytest.mark.parametrize("factory", FACTORIES)
def test_filter_precedes_missing_history_and_suppression_precedes_filter(factory):
    case = load_document(json.loads(FIXTURE.read_text(encoding="utf-8"))).cases[1].to_dict()
    case["facts"][2]["lastChangedEpisodeNo"] = 3
    slot = deepcopy(case["facts"][0]["slot"])
    case["suppressions"] = [
        {
            "suppressionRef": "sup1",
            "slot": slot,
            "reason": "USER_DELETE",
            "appliesTo": "ALL_EPISODES",
            "recordedRevision": 1,
        }
    ]
    model = factory(Case.model_validate(case), extended=True)
    assert model.validate()["conforms"]
    social = Query(
        queryRef="social",
        mode="READER_KNOWLEDGE",
        cutoffEpisodeNo=2,
        storyContextId=None,
        axis="SOCIAL",
    )
    result = model.query(social)
    assert result["status"] == "READY"
    assert [fact["factRef"] for fact in result["facts"]] == ["f4"]
    assert {row["factRef"]: row["reason"] for row in result["excludedFacts"]} == {
        "f1": "USER_SUPPRESSED",
        "f2": "QUERY_FILTER",
        "f3": "QUERY_FILTER",
    }
    attitude = social.model_copy(update={"axis": "ATTITUDE"})
    assert model.query(attitude)["reason"] == "HISTORY_NOT_AVAILABLE"


@pytest.mark.parametrize("factory", FACTORIES)
def test_symmetric_slot_is_returned_once_from_either_perspective(factory):
    case = load_document(json.loads(FIXTURE.read_text(encoding="utf-8"))).cases[1]
    model = factory(case, extended=True)
    for perspective in ("INCOMING", "OUTGOING"):
        query = Query(
            queryRef="mutual",
            mode="READER_KNOWLEDGE",
            cutoffEpisodeNo=3,
            storyContextId=None,
            nodeRef="harin",
            perspective=perspective,
        )
        assert [fact["factRef"] for fact in model.query(query)["facts"]] == ["f3"]
