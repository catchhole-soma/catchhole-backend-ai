"""Whole-fixture oracles, provenance integrity, and immutable evidence boundaries."""

import json
from copy import deepcopy
from hashlib import sha256
from itertools import combinations
from pathlib import Path

import pytest

from scripts.world_owl_experiment import run
from scripts.world_owl_experiment.contract import InputError, parse_document
from scripts.world_owl_experiment.evaluate import digest, evaluate
from scripts.world_owl_experiment.policy import prepare
from scripts.world_owl_experiment.proofs import ProofLimitError, minimal_supports

FIXTURES = Path(__file__).parent / "fixtures"
INPUT = FIXTURES / "world_owl_cases.json"
ORACLE = FIXTURES / "world_owl_oracle.json"


@pytest.fixture(scope="module")
def document():
    return json.loads(INPUT.read_text())


@pytest.fixture(scope="module")
def oracle():
    return json.loads(ORACLE.read_text())


@pytest.fixture(scope="module")
def computed(document):
    return run.compute(document)


def test_all_saved_meanings_match_independently_authored_answers(computed, oracle, document):
    actual, measurements, ontologies = computed
    report = evaluate(actual, oracle, document)
    assert report["failed"] == 0, json.dumps(report, ensure_ascii=False, indent=2)
    assert report["passed"] == len(oracle["cases"]) >= 25
    assert len(measurements) == len(ontologies) == len(actual)
    assert all(row["engine"] for row in measurements)
    assert all("rdf:RDF" in text for text in ontologies.values())


def test_input_order_does_not_change_semantics_or_provenance(document, computed):
    scrambled = deepcopy(document)
    scrambled["cases"].reverse()
    for case in scrambled["cases"]:
        case["statements"].reverse()
        case["sources"].reverse()
        for row in case["statements"]:
            row["sourceIds"].reverse()
    before = {row["caseId"]: row for row in computed[0]}
    after = {row["caseId"]: row for row in run.compute(scrambled)[0]}
    assert after == before


def test_every_proof_maps_only_to_eligible_original_sources(document, computed):
    by_id = {row["caseId"]: row for row in document["cases"]}
    for actual in computed[0]:
        case = by_id[actual["caseId"]]
        statements, _, eligible_sources, eligible_map = prepare(case)
        statement_map = {row["id"]: row for row in statements}
        assert actual["eligibleStatementIds"] == sorted(statement_map)
        all_supports = actual["conflictSets"] + [
            support for row in actual["queries"] for support in row["supports"]
        ]
        for support in all_supports:
            assert set(support["axiomIds"]).isdisjoint(support["factIds"])
            assert set(support["axiomIds"]) | set(support["factIds"]) == set(
                support["statementIds"]
            )
            assert support["sourceAlternatives"] == [
                {"statementId": key, "sourceIds": eligible_map[key]}
                for key in support["statementIds"]
            ]
            assert support["sourceIds"] == sorted(
                {key for row in support["sourceAlternatives"] for key in row["sourceIds"]}
            )
            assert [source["id"] for source in support["sources"]] == support["sourceIds"]
            for source in support["sources"]:
                original = eligible_sources[source["id"]]
                assert source["text"] == original["text"]
                assert source["sha256"] == original["sha256"]
                assert source["episodeNo"] <= case["scope"]["cutoffEpisode"]
                assert source["textStatus"] == (
                    "PURGED" if original["text"] is None else "AVAILABLE"
                )


def test_all_proofs_are_sufficient_and_minimal_in_fresh_engine(document, computed):
    from scripts.world_owl_experiment.engine import Engine

    actual_by_id = {row["caseId"]: row for row in computed[0]}
    for case in document["cases"]:
        statements, *_ = prepare(case)
        engine = Engine(case["vocabulary"], case["scope"], statements)
        actual = actual_by_id[case["caseId"]]
        tasks = [
            (support, lambda ids, engine=engine: not engine.consistent(ids))
            for support in actual["conflictSets"]
        ]
        for query in actual["queries"]:
            if query["verdict"] == "UNSATISFIABLE":
                predicate = lambda ids, q=query["query"], engine=engine: (
                    not engine.satisfiable(ids, q["class"])
                )
            else:
                predicate = (
                    lambda ids, q=query["query"], neg=query["verdict"] == "REFUTED", engine=engine: (
                        engine.entailed(ids, q, negative=neg)
                    )
                )
            tasks.extend((support, predicate) for support in query["supports"])
        for support, predicate in tasks:
            ids = frozenset(support["statementIds"])
            assert predicate(ids)
            assert all(not predicate(ids - {key}) for key in ids)


def test_proof_enumerator_is_complete_against_independent_powerset():
    # An independent monotone truth table has overlapping, disjoint and empty
    # supports. This tests enumeration without using the same search algorithm.
    for expected in ([{"a", "b"}, {"a", "c"}, {"d"}], [set()], [], [{"a", "b", "c", "d"}]):
        universe = {"a", "b", "c", "d"}
        predicate = lambda selected, expected=expected: any(group <= selected for group in expected)
        brute = []
        for length in range(len(universe) + 1):
            for combination in combinations(sorted(universe), length):
                selected = set(combination)
                if predicate(selected) and not any(set(group) <= selected for group in brute):
                    brute.append(list(combination))
        assert minimal_supports(universe, predicate) == sorted(brute)


def test_proof_budget_never_returns_a_partial_set_as_complete():
    with pytest.raises(ProofLimitError):
        minimal_supports({"a", "b"}, lambda ids: bool(ids), max_nodes=1)


def test_all_168_four_atom_monotone_support_families():
    # Independently enumerate every antichain of the four-element powerset.
    # Each antichain uniquely describes a monotone predicate's minimal supports.
    atoms = ("a", "b", "c", "d")
    count = 0
    for family_bits in range(1 << 16):
        family = [mask for mask in range(16) if family_bits & (1 << mask)]
        if any(left != right and left & right == left for left in family for right in family):
            continue
        expected = sorted(
            [[atom for index, atom in enumerate(atoms) if mask & (1 << index)] for mask in family]
        )
        predicate = lambda selected, expected=expected: any(
            set(group) <= selected for group in expected
        )
        assert minimal_supports(atoms, predicate) == expected
        count += 1
    assert count == 168


def test_real_budget_failure_is_error_not_unknown(document):
    single = {"schemaVersion": 1, "cases": [document["cases"][0]]}
    actuals, _, _ = run.compute(single, max_checks=1)
    assert actuals[0]["outcome"] == "ERROR"
    assert not actuals[0]["explanationComplete"]
    assert actuals[0]["queries"] == []


def test_oracle_is_not_opened_until_actual_has_been_saved(tmp_path):
    output = tmp_path / "record"
    with pytest.raises(FileNotFoundError):
        run.execute(INPUT, tmp_path / "missing-oracle.json", output)
    assert (output / "actual.json").is_file()
    assert json.loads((output / "actual.json").read_text())


def test_saved_actual_corruption_is_rejected(tmp_path, monkeypatch):
    original_save = run.save

    def corrupted(path, value):
        if path.name == "actual.json":
            value = []
        original_save(path, value)

    monkeypatch.setattr(run, "save", corrupted)
    with pytest.raises(ValueError, match="persisted"):
        run.execute(INPUT, ORACLE, tmp_path / "corrupted")


def test_oracle_changes_cannot_change_actual_and_records_are_immutable(tmp_path, oracle, computed):
    tampered = deepcopy(oracle)
    tampered["cases"][0]["outcome"] = "INCONSISTENT"
    wrong = tmp_path / "wrong.json"
    wrong.write_text(json.dumps(tampered))
    output = tmp_path / "wrong-result"
    manifest = run.execute(INPUT, wrong, output)
    assert manifest["evaluation"]["failed"] == 1
    assert digest(json.loads((output / "actual.json").read_text())) == digest(computed[0])
    with pytest.raises(FileExistsError):
        run.execute(INPUT, ORACLE, output)


@pytest.mark.parametrize(
    "damage",
    ["verdict", "missing_support", "duplicate_support", "conflict", "missing_case", "incomplete"],
)
def test_evaluator_catches_semantic_or_completeness_damage(computed, oracle, document, damage):
    actual = deepcopy(computed[0])
    if damage == "missing_case":
        actual.pop()
    elif damage == "incomplete":
        actual[0]["explanationComplete"] = False
    elif damage == "conflict":
        actual[0]["conflictSets"] = [["fake"]]
    else:
        query = next(query for case in actual for query in case["queries"] if query["supports"])
        if damage == "verdict":
            query["verdict"] = "UNKNOWN"
        elif damage == "missing_support":
            query["supports"].pop()
        else:
            query["supports"].append(deepcopy(query["supports"][0]))
    try:
        assert evaluate(actual, oracle, document)["failed"] >= 1
    except ValueError:
        assert damage in {"duplicate_support", "missing_case"}


@pytest.mark.parametrize(
    "damage",
    [
        "extra",
        "oracle",
        "boolean_revision",
        "unknown_class",
        "unsupported",
        "bad_hash",
        "duplicate_source",
        "unknown_source",
        "bad_interval",
        "nonbool_confirmed",
        "empty_source",
        "duplicate_query",
    ],
)
def test_input_rejects_malformed_or_out_of_subset_data(document, damage):
    invalid = deepcopy(document)
    case = invalid["cases"][0]
    if damage == "extra":
        case["unexpected"] = 1
    elif damage == "oracle":
        invalid["expected"] = {"answer": "ENTAILED"}
    elif damage == "boolean_revision":
        case["scope"]["snapshotRevision"] = True
    elif damage == "unknown_class":
        case["statements"][0]["axiom"] = {
            "type": "classAssertion",
            "individual": case["vocabulary"]["individuals"][0],
            "class": "Undeclared",
        }
    elif damage == "unsupported":
        case["queries"][0] = {
            "id": "bad",
            "type": "satisfiable",
            "class": {"op": "min", "cardinality": 2},
        }
    elif damage == "bad_hash":
        case["sources"][0]["sha256"] = "0" * 64
    elif damage == "duplicate_source":
        case["sources"].append(deepcopy(case["sources"][0]))
    elif damage == "unknown_source":
        case["statements"][0]["sourceIds"] = ["missing"]
    elif damage == "bad_interval":
        case["statements"][0]["validTo"] = case["statements"][0]["validFrom"]
    elif damage == "nonbool_confirmed":
        case["statements"][0]["confirmed"] = "true"
    elif damage == "empty_source":
        case["statements"][0]["sourceIds"] = []
    else:
        case["queries"].append(deepcopy(case["queries"][0]))
    with pytest.raises(InputError):
        parse_document(invalid)


def test_future_definition_cannot_change_earlier_reader_answer(document):
    from scripts.world_owl_experiment.reasoning import run_case

    case = deepcopy(document["cases"][0])
    baseline = run_case(case)[0]
    vocabulary = case["vocabulary"]
    individual = vocabulary["individuals"][0]
    cls = vocabulary["classes"][0]
    source = deepcopy(case["sources"][0])
    source.update(
        id="futureEvidence",
        episodeNo=case["scope"]["cutoffEpisode"] + 1,
        text="미래 회차에서만 밝혀진 부정 사실이다.",
    )
    source["sha256"] = sha256(source["text"].encode()).hexdigest()
    row = deepcopy(case["statements"][0])
    row.update(
        id="futureRule",
        sourceIds=[source["id"]],
        axiom={
            "type": "classAssertion",
            "individual": individual,
            "class": {"op": "not", "arg": cls},
        },
    )
    case["sources"].append(source)
    case["statements"].append(row)
    after = run_case(case)[0]
    assert after["queries"] == baseline["queries"]
    assert after["outcome"] == baseline["outcome"]
    assert {"id": "futureRule", "reason": "NO_ELIGIBLE_SOURCE"} in after["excludedStatements"]


def test_contract_does_not_mutate_oracle_or_caller_input(document):
    original = deepcopy(document)
    parsed = parse_document(document)
    parsed[0]["sources"][0]["text"] = "modified copy"
    assert document == original


@pytest.mark.parametrize(
    "damage",
    [
        "source_id",
        "source_alternative",
        "quote",
        "hash",
        "episode",
        "purged",
        "fact_split",
        "scope",
        "eligible",
        "query",
    ],
)
def test_evaluator_rejects_broken_provenance_even_when_logical_ids_match(
    document, computed, oracle, damage
):
    actual = deepcopy(computed[0])
    case = actual[0]
    query = next(row for row in case["queries"] if row["supports"])
    proof = query["supports"][0]
    if damage == "source_id":
        proof["sourceIds"] = ["futureSource"]
    elif damage == "source_alternative":
        proof["sourceAlternatives"][0]["sourceIds"] = []
    elif damage == "quote":
        proof["sources"][0]["text"] = "원문에 없는 조작된 인용"
    elif damage == "hash":
        proof["sources"][0]["sha256"] = "0" * 64
    elif damage == "episode":
        proof["sources"][0]["episodeNo"] = 999
    elif damage == "purged":
        proof["sources"][0]["textStatus"] = "PURGED"
    elif damage == "fact_split":
        proof["axiomIds"], proof["factIds"] = [], proof["statementIds"]
    elif damage == "scope":
        case["scope"]["workId"] = "different-work"
    elif damage == "eligible":
        case["eligibleStatementIds"] = []
    else:
        query["query"]["individual"] = "someoneElse"
    report = evaluate(actual, oracle, document)
    assert report["failed"] == 1
    assert "provenance invalid" in report["failures"][0]["reason"]


@pytest.mark.parametrize("damage", ["operator", "axiom_type", "modality"])
def test_unhashable_json_tags_are_input_errors(document, damage):
    invalid = deepcopy(document)
    row = invalid["cases"][0]["statements"][0]
    if damage == "modality":
        row["modality"] = []
    elif damage == "axiom_type":
        row["axiom"]["type"] = []
    else:
        row["axiom"] = {"type": "subclass", "sub": {"op": []}, "super": "Mage"}
    with pytest.raises(InputError):
        parse_document(invalid)


def test_case_ids_cannot_overwrite_ontology_files_on_macos(document):
    invalid = deepcopy(document)
    invalid["cases"][0]["caseId"] = "World"
    invalid["cases"][1]["caseId"] = "world"
    with pytest.raises(InputError, match="case-insensitive"):
        parse_document(invalid)


def test_ontology_export_bytes_are_rechecked_before_evaluation(tmp_path, monkeypatch):
    original = Path.write_text

    def corrupt_ontology(path, data, *args, **kwargs):
        return original(path, "broken export" if path.suffix == ".owl" else data, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", corrupt_ontology)
    with pytest.raises(ValueError, match="persisted ontology"):
        run.execute(INPUT, ORACLE, tmp_path / "corrupted-ontology")


def test_extended_negation_conflict_repair_and_cycles():
    document = json.loads((FIXTURES / "world_owl_extension_cases.json").read_text())
    oracle = json.loads((FIXTURES / "world_owl_extension_oracle.json").read_text())
    actual, _, _ = run.compute(document)
    report = evaluate(actual, oracle, document)
    assert report["failed"] == 0, json.dumps(report, ensure_ascii=False, indent=2)
    assert report["caseCount"] == 6
    assert report["queryCount"] == 27
