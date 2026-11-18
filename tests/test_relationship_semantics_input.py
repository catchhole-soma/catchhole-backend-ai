"""Structural loader checks; no model, network, database or semantic execution."""

import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import pytest
from pydantic import ValidationError

from scripts.relationship_semantics_experiment.input import (
    Dataset,
    Query,
    input_document,
    load_document,
    load_path,
)

FIXTURE = Path(__file__).parent / "fixtures" / "relationship_semantics_cases.json"


@pytest.fixture
def document():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


class OracleGuard(Mapping):
    """Raise if an implementation reads even one expected/consumer value."""

    def __init__(self, value):
        self.value = value

    def __iter__(self):
        return iter(self.value)

    def __len__(self):
        return len(self.value)

    def __getitem__(self, key):
        if key in {"expected", "consumerExamples", "expectedText"}:
            raise AssertionError("oracle value was accessed")
        value = self.value[key]
        if isinstance(value, Mapping):
            return OracleGuard(value)
        if isinstance(value, list):
            return [OracleGuard(item) if isinstance(item, Mapping) else item for item in value]
        return value


def test_base_fixture_loads_without_reading_oracle_values(document):
    dataset = load_document(OracleGuard(document))
    assert len(dataset.cases) == 10
    assert sum(len(case.queries) for case in dataset.cases) == 11
    first = dataset.cases[0]
    assert first.title == document["cases"][0]["title"]
    assert first.purpose == document["cases"][0]["purpose"]
    assert first.product_question_id == "P1"
    assert first.facts[0].slot.to_dict() == document["cases"][0]["facts"][0]["slot"]
    assert (
        first.queries[0].axis is first.queries[0].node_ref is first.queries[0].perspective is None
    )
    assert load_path(FIXTURE) == dataset


def test_oracle_removal_and_replacement_do_not_change_execution_document(document):
    expected = input_document(document)
    for case in document["cases"]:
        for query in case["queries"]:
            query.pop("expected")
    assert input_document(document) == expected
    for case in document["cases"]:
        for query in case["queries"]:
            query["expected"] = object()
            query["consumerExamples"] = object()
            query["expectedText"] = object()
    assert input_document(OracleGuard(document)) == expected


def test_execution_json_round_trips_and_preserves_source_order(document, tmp_path):
    dataset = load_document(document)
    execution = dataset.to_dict()
    assert set(execution) == {"schemaVersion", "synthetic", "cases"}
    assert load_document(execution) == dataset
    assert Dataset.model_validate(execution) == dataset
    assert input_document(execution) == execution
    path = tmp_path / "inputs.json"
    path.write_text(json.dumps(execution, ensure_ascii=False), encoding="utf-8")
    assert load_path(path) == dataset
    assert execution["cases"][0]["sources"] == document["cases"][0]["sources"]
    assert all("expected" not in q for c in execution["cases"] for q in c["queries"])


def test_models_are_frozen_with_immutable_collections(document):
    case = load_document(document).cases[0]
    with pytest.raises(ValidationError, match="frozen"):
        case.case_id = "changed"
    assert isinstance(case.nodes, tuple)
    assert isinstance(case.claims[0].subject_candidate_refs, tuple)
    exported = case.to_dict()
    exported["nodes"][0]["name"] = "changed"
    assert case.nodes[0].name != "changed"


def test_query_filter_fields_accept_snake_case_and_preserve_json_aliases():
    query = Query(
        query_ref="incoming",
        mode="READER_KNOWLEDGE",
        cutoff_episode_no=3,
        story_context_id=None,
        axis="ATTITUDE",
        node_ref="doyun",
        perspective="INCOMING",
    )
    assert query.to_dict() == {
        "queryRef": "incoming",
        "mode": "READER_KNOWLEDGE",
        "cutoffEpisodeNo": 3,
        "storyContextId": None,
        "axis": "ATTITUDE",
        "nodeRef": "doyun",
        "perspective": "INCOMING",
    }


@pytest.mark.parametrize(
    ("path", "value"),
    [
        (("schemaVersion",), True),
        (("schemaVersion",), "1"),
        (("schemaVersion",), 1.0),
        (("schemaVersion",), 2),
        (("synthetic",), 1),
        (("synthetic",), "true"),
        (("synthetic",), False),
        (("cases", 0, "nodes", 0, "name"), 3),
        (("cases", 0, "sources", 0, "episodeNo"), True),
        (("cases", 0, "sources", 0, "episodeNo"), "1"),
        (("cases", 0, "facts", 0, "slot", "kind"), 3),
        (("cases", 0, "facts", 0, "slot", "axis"), "OTHER"),
        (("cases", 0, "facts", 0, "slot", "direction"), "BOTH"),
        (("cases", 0, "queries", 0, "mode"), "UNRECOGNIZED"),
        (("cases", 0, "queries", 0, "perspective"), "SIDEWAYS"),
        (("cases", 0, "claims", 0, "subjectCandidateRefs"), [1]),
        (("cases", 0, "snapshotThroughEpisodeNo"), 0),
        (("cases", 0, "snapshotRevision"), -1),
        (("cases", 0, "sources", 0, "episodeNo"), 0),
        (("cases", 0, "facts", 0, "knownFromEpisodeNo"), -1),
        (("cases", 0, "facts", 0, "lastChangedEpisodeNo"), 0),
        (("cases", 0, "queries", 0, "cutoffEpisodeNo"), 0),
        (("cases", 8, "suppressions", 0, "recordedRevision"), -1),
        (("cases", 0, "caseId"), " "),
        (("cases", 0, "nodes", 0, "name"), "\n"),
        (("cases", 0, "sources", 0, "text"), "\t"),
        (("cases", 0, "sources", 0, "quote"), ""),
        (("cases", 0, "claims", 0, "subjectCandidateRefs"), [" "]),
    ],
)
def test_invalid_structural_scalars_and_enums_are_rejected(document, path, value):
    target = document
    for segment in path[:-1]:
        target = target[segment]
    target[path[-1]] = value
    with pytest.raises(ValueError):
        load_document(document)


@pytest.mark.parametrize(
    "path",
    [(), ("cases", 0), ("cases", 0, "queries", 0), ("cases", 0, "facts", 0, "slot")],
)
def test_unknown_fields_are_not_silently_dropped(document, path):
    target = document
    for segment in path:
        target = target[segment]
    target["unsupportedNewField"] = "must not disappear"
    with pytest.raises(ValueError, match="unsupported field"):
        load_document(document)


@pytest.mark.parametrize("field", ["schemaVersion", "synthetic"])
def test_input_requires_explicit_version_and_synthetic_scope(document, field):
    document.pop(field)
    with pytest.raises(ValueError, match="required"):
        load_document(document)


def test_duplicate_case_identity_is_rejected(document):
    document["cases"][1]["caseId"] = document["cases"][0]["caseId"]
    with pytest.raises(ValueError, match="duplicate caseId"):
        load_document(document)


def test_source_whitespace_and_independent_case_local_refs_are_preserved(document):
    source = document["cases"][0]["sources"][0]
    source["text"] = "  " + source["text"] + "\n"
    source["quote"] = " " + source["quote"] + " "
    dataset = load_document(document)
    assert dataset.cases[0].sources[0].text == source["text"]
    assert dataset.cases[0].sources[0].quote == source["quote"]
    assert dataset.cases[0].facts[0].fact_ref == dataset.cases[1].facts[0].fact_ref == "f1"


def test_semantic_errors_remain_visible_to_independent_b_and_c_validators(document):
    changed = deepcopy(document)
    case = changed["cases"][0]
    case["nodes"][0]["workId"] = "another-work"
    case["claims"][0]["sourceRef"] = "missing"
    case["facts"][0]["knownFromEpisodeNo"] = 5
    case["facts"][0]["lastChangedEpisodeNo"] = 1
    case["facts"][0]["slot"]["kind"] = "NOT_IN_PRODUCT_VOCABULARY"
    case["facts"].append(deepcopy(case["facts"][0]))
    loaded = load_document(changed).cases[0]
    assert loaded.nodes[0].work_id == "another-work"
    assert loaded.claims[0].source_ref == "missing"
    assert loaded.facts[0].known_from_episode_no == 5
    assert loaded.facts[0].slot.kind == "NOT_IN_PRODUCT_VOCABULARY"
    assert len(loaded.facts) == 3
