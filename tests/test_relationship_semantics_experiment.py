"""Independent checks of executed B/C results, oracle isolation and policy edges."""

import builtins
import io
import json
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path

import pytest

from scripts.relationship_semantics_experiment import run as runner
from scripts.relationship_semantics_experiment.evaluate import evaluate_result, evaluate_results
from scripts.relationship_semantics_experiment.input import Case, load_document, load_path
from scripts.relationship_semantics_experiment.rdf_model import RdfModel
from scripts.relationship_semantics_experiment.typed_model import TypedModel

FIXTURE = Path(__file__).parent / "fixtures" / "relationship_semantics_cases.json"
FACTORIES = {"B": TypedModel, "C": RdfModel}


@pytest.fixture(scope="module")
def dataset():
    return load_path(FIXTURE)


@pytest.fixture(scope="module")
def oracle():
    # Only evaluation/isolation tests receive this object; models receive Case.
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


@pytest.fixture(params=FACTORIES, ids=FACTORIES)
def factory(request):
    return FACTORIES[request.param]


def execute(factory, case):
    model = factory(case, extended=False)
    validation = model.validate()
    assert validation["conforms"], validation
    assert not validation["violations"]
    return model, [model.query(query) for query in case.queries]


@pytest.fixture(scope="module")
def base_executions(dataset):
    executions = {}
    for name, factory in FACTORIES.items():
        executions[name] = []
        for case in dataset.cases:
            model, results = execute(factory, case)
            assert model.to_case().to_dict() == case.to_dict(), (name, case.case_id)
            executions[name].extend(results)
    return executions


@pytest.mark.parametrize("implementation", FACTORIES)
def test_all_base_queries_execute_and_match_handwritten_oracle(
    oracle, base_executions, implementation
):
    actual = base_executions[implementation]
    assert len(actual) == 11
    evaluation = evaluate_results(oracle, actual)
    assert evaluation["queryCount"] == 11
    assert evaluation["passed"], evaluation
    assert sum(result["status"] == "READY" for result in actual) == 10
    assert sum(result["status"] == "UNSUPPORTED" for result in actual) == 1


def test_independent_engines_return_identical_full_results(base_executions):
    assert base_executions["B"] == base_executions["C"]


def test_case_local_ids_do_not_share_suppression_or_state(factory, dataset):
    first, initial = execute(factory, dataset.cases[0])
    suppressed, hidden = execute(factory, dataset.cases[8])
    assert initial[0]["facts"][0]["factRef"] == "f1"
    assert hidden[0]["facts"] == []
    assert hidden[0]["excludedFacts"] == [{"factRef": "f1", "reason": "USER_SUPPRESSED"}]
    assert first.query(dataset.cases[0].queries[0]) == initial[0]
    assert suppressed.to_case().to_dict() == dataset.cases[8].to_dict()


class GuardedDocument(Mapping):
    def __init__(self, document):
        self.document = document

    def __iter__(self):
        return iter(self.document)

    def __len__(self):
        return len(self.document)

    def __getitem__(self, key):
        if key in {"expected", "consumerExamples", "expectedText"}:
            raise AssertionError("execution accessed oracle values")
        value = self.document[key]
        if isinstance(value, Mapping):
            return GuardedDocument(value)
        if isinstance(value, list):
            return [GuardedDocument(item) if isinstance(item, Mapping) else item for item in value]
        return value


def test_oracle_deletion_mutation_and_access_guard_leave_executed_output_unchanged(
    factory, oracle, monkeypatch
):
    original = deepcopy(oracle)
    original["cases"] = original["cases"][:1]

    def guarded_open(original_open):
        def open_without_oracle(file, *args, **kwargs):
            if isinstance(file, (str, Path)) and Path(file).resolve() == FIXTURE.resolve():
                raise AssertionError("execution reopened the oracle fixture")
            return original_open(file, *args, **kwargs)

        return open_without_oracle

    monkeypatch.setattr(builtins, "open", guarded_open(builtins.open))
    monkeypatch.setattr(io, "open", guarded_open(io.open))
    _, baseline = execute(factory, load_document(original).cases[0])
    assert evaluate_results(original, baseline)["passed"]

    removed = deepcopy(original)
    for query in removed["cases"][0]["queries"]:
        query.pop("expected")
    _, without_oracle = execute(factory, load_document(removed).cases[0])
    assert without_oracle == baseline

    changed = deepcopy(original)
    changed["cases"][0]["queries"][0]["expected"]["factRefs"] = []
    _, with_wrong_oracle = execute(factory, load_document(changed).cases[0])
    assert with_wrong_oracle == baseline
    assert not evaluate_results(changed, with_wrong_oracle)["passed"]

    _, guarded = execute(factory, load_document(GuardedDocument(original)).cases[0])
    assert guarded == baseline


@pytest.mark.parametrize(
    "defect",
    [
        "axis",
        "kind",
        "work",
        "direction",
        "endpoint",
        "status",
        "validity",
        "knowledge",
        "changed",
        "support_claim",
        "support_source",
        "extra_fact",
        "duplicate_fact",
        "missing_fact",
        "duplicate_support",
        "extra_held",
        "reason",
        "missing_field",
    ],
)
def test_evaluator_detects_full_semantic_output_changes(oracle, base_executions, defect):
    actual = deepcopy(base_executions["B"][0])
    first = actual["facts"][0]
    if defect == "axis":
        first["slot"]["axis"] = "SOCIAL"
    elif defect == "kind":
        first["slot"]["kind"] = "FRIENDSHIP"
    elif defect == "work":
        first["slot"]["workId"] = "other-work"
    elif defect == "direction":
        first["slot"]["direction"] = "MUTUAL"
    elif defect == "endpoint":
        first["slot"]["sourceNodeRef"], first["slot"]["targetNodeRef"] = (
            first["slot"]["targetNodeRef"],
            first["slot"]["sourceNodeRef"],
        )
    elif defect == "status":
        first["status"] = "ENDED"
    elif defect == "validity":
        first["factValidity"] = "RETRACTED"
    elif defect == "knowledge":
        first["knownFromEpisodeNo"] = 2
    elif defect == "changed":
        first["lastChangedEpisodeNo"] = 2
    elif defect == "support_claim":
        first["supportClaimRefs"] = ["c2"]
    elif defect == "support_source":
        first["supportSourceRefs"] = ["s2"]
    elif defect == "duplicate_support":
        first["supportSourceRefs"] *= 2
    elif defect in {"duplicate_fact", "extra_fact"}:
        extra = deepcopy(first)
        if defect == "extra_fact":
            extra["factRef"] = "invented"
        actual["facts"].append(extra)
    elif defect == "missing_fact":
        actual["facts"].pop()
    elif defect == "extra_held":
        actual["heldClaimRefs"] = ["c1"]
    elif defect == "missing_field":
        first.pop("supportSourceRefs")
    else:
        actual["reason"] = "UNSUPPORTED_QUERY"
    case = oracle["cases"][0]
    assert not evaluate_result(case, case["queries"][0], actual)["passed"]


def test_evaluator_compares_unordered_sets_and_symmetric_slots_without_rewriting_inputs(
    oracle, base_executions
):
    original = base_executions["B"][1]
    actual = deepcopy(original)
    mutual = next(fact for fact in actual["facts"] if fact["slot"]["direction"] == "MUTUAL")
    mutual["slot"]["sourceNodeRef"], mutual["slot"]["targetNodeRef"] = (
        mutual["slot"]["targetNodeRef"],
        mutual["slot"]["sourceNodeRef"],
    )
    actual["facts"].reverse()
    before_evaluation = deepcopy(actual)
    case = oracle["cases"][1]
    assert evaluate_result(case, case["queries"][0], actual)["passed"]
    assert actual == before_evaluation
    assert original != actual


@pytest.mark.parametrize("defect", ["missing", "duplicate", "unknown"])
def test_evaluator_detects_query_inventory_changes(oracle, base_executions, defect):
    actual = deepcopy(base_executions["B"])
    if defect == "missing":
        actual.pop()
    elif defect == "duplicate":
        actual.append(deepcopy(actual[0]))
    else:
        actual[0]["caseId"] = "unknown-case"
    assert not evaluate_results(oracle, actual)["passed"]


@pytest.mark.parametrize(
    ("case_index", "path", "replacement"),
    [
        (0, ("facts", 0, "slot", "targetNodeRef"), "missing"),
        (0, ("facts", 0, "slot", "targetNodeRef"), "harin"),
        (0, ("facts", 0, "slot", "targetNodeRef"), "doyun"),
        (0, ("claims", 0, "sourceRef"), "missing"),
        (0, ("facts", 0, "supportClaimRefs"), ["missing"]),
        (0, ("nodes", 0, "workId"), "other-work"),
        (0, ("sources", 0, "workId"), "other-work"),
        (0, ("facts", 0, "slot", "workId"), "other-work"),
        (0, ("sources", 0, "quote"), "not present in source"),
        (0, ("sources", 0, "episodeNo"), 4),
        (0, ("facts", 0, "knownFromEpisodeNo"), 3),
        (0, ("facts", 0, "supportClaimRefs"), []),
        (0, ("facts", 1, "factRef"), "f1"),
        (0, ("claims", 0, "subjectCandidateRefs"), ["doyun", "harin"]),
        (0, ("claims", 0, "curationStatus"), "HOLD"),
        (0, ("claims", 0, "temporalScope"), "PAST"),
        (0, ("facts", 0, "slot", "kind"), "UNDECLARED_KIND"),
        (6, ("facts", 0, "endClaimRefs"), []),
        (6, ("facts", 0, "lastChangedEpisodeNo"), 1),
        (7, ("facts", 0, "correctionSourceRefs"), []),
        (7, ("claims", 0, "curationStatus"), "CONFIRMED"),
        (7, ("facts", 0, "slot", "targetNodeRef"), "harin"),
        (7, ("facts", 0, "lastChangedEpisodeNo"), 1),
        (8, ("suppressions", 0, "recordedRevision"), 3),
        (8, ("suppressions", 0, "slot", "targetNodeRef"), "missing"),
    ],
)
def test_both_models_reject_semantic_damage(factory, dataset, case_index, path, replacement):
    raw = dataset.cases[case_index].to_dict()
    target = raw
    for part in path[:-1]:
        target = target[part]
    target[path[-1]] = replacement
    # These remain structurally valid inputs, so validation must be done by B/C.
    model = factory(Case.model_validate(raw), extended=False)
    report = model.validate()
    assert report["conforms"] is False, (path, replacement, report)
    assert report["violations"]
    with pytest.raises(ValueError):
        model.query(Case.model_validate(raw).queries[0])


@pytest.mark.parametrize(
    ("mode", "cutoff", "context", "reason"),
    [
        ("STORY_TIME", 4, "first-expedition", "STORY_TIME_RECONSTRUCTION_NOT_SUPPORTED"),
        ("READER_KNOWLEDGE", 4, None, "QUERY_BEYOND_SNAPSHOT"),
        ("READER_KNOWLEDGE", 1, None, "HISTORY_NOT_AVAILABLE"),
    ],
)
def test_unsupported_query_priority(factory, dataset, mode, cutoff, context, reason):
    raw = dataset.cases[6].to_dict()
    raw["queries"] = [
        {"queryRef": "policy", "mode": mode, "cutoffEpisodeNo": cutoff, "storyContextId": context}
    ]
    case = Case.model_validate(raw)
    _, results = execute(factory, case)
    result = results[0]
    assert result["status"] == "UNSUPPORTED" and result["reason"] == reason
    assert result["facts"] == result["heldClaimRefs"] == []
    assert result["excludedFacts"] == [{"factRef": "f1", "reason": "UNSUPPORTED_QUERY"}]


@pytest.mark.parametrize(
    ("known_from", "suppressed", "reason"),
    [(3, True, "FUTURE_KNOWLEDGE"), (1, True, "USER_SUPPRESSED"), (1, False, "RETRACTED_FACT")],
)
def test_fact_exclusion_priority_precedes_history_for_hidden_facts(
    factory, dataset, known_from, suppressed, reason
):
    raw = dataset.cases[7].to_dict()
    raw["facts"][0]["knownFromEpisodeNo"] = known_from
    if suppressed:
        raw["suppressions"] = [
            {
                "suppressionRef": "sup1",
                "slot": deepcopy(raw["facts"][0]["slot"]),
                "reason": "USER_DELETE",
                "appliesTo": "ALL_EPISODES",
                "recordedRevision": 2,
            }
        ]
    raw["queries"][0]["cutoffEpisodeNo"] = 1
    _, results = execute(factory, Case.model_validate(raw))
    assert results[0]["status"] == "READY"
    assert results[0]["reason"] is None
    assert results[0]["facts"] == []
    assert results[0]["excludedFacts"] == [{"factRef": "f1", "reason": reason}]


def test_hold_claims_are_limited_to_reader_knowledge(factory, dataset):
    raw = dataset.cases[5].to_dict()
    raw["sources"][0]["episodeNo"] = 1
    raw["sources"][1]["episodeNo"] = 3
    raw["queries"] = [
        {
            "queryRef": "q1",
            "mode": "READER_KNOWLEDGE",
            "cutoffEpisodeNo": 1,
            "storyContextId": None,
        },
        {
            "queryRef": "q3",
            "mode": "READER_KNOWLEDGE",
            "cutoffEpisodeNo": 3,
            "storyContextId": None,
        },
    ]
    _, results = execute(factory, Case.model_validate(raw))
    assert results[0]["heldClaimRefs"] == ["c1"]
    assert results[1]["heldClaimRefs"] == ["c1", "c2"]
    assert all(result["facts"] == [] for result in results)


def write_document(path, document):
    path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")


def test_run_saves_actuals_before_oracle_read_and_evaluates_saved_json(
    oracle, tmp_path, monkeypatch
):
    small = deepcopy(oracle)
    small["cases"] = small["cases"][:1]
    inputs, expected, output = tmp_path / "input.json", tmp_path / "oracle.json", tmp_path / "out"
    write_document(inputs, load_document(small).to_dict())
    write_document(expected, small)
    original_read, original_write = Path.read_text, runner.write_json
    oracle_reads = []

    def guarded_read(path, *args, **kwargs):
        if path == expected:
            for name in ("B", "C"):
                saved = json.loads(original_read(output / f"actual-{name}.json", encoding="utf-8"))
                assert saved["results"] and saved["validations"][0]["conforms"]
            oracle_reads.append(path)
        return original_read(path, *args, **kwargs)

    def damaged_saved_actual(path, value):
        original_write(path, value)
        if path.name == "actual-B.json":
            saved = json.loads(original_read(path, encoding="utf-8"))
            saved["results"][0]["facts"][0]["status"] = "ENDED"
            original_write(path, saved)

    monkeypatch.setattr(Path, "read_text", guarded_read)
    monkeypatch.setattr(runner, "write_json", damaged_saved_actual)
    summary = runner.run(inputs, output, oracle_path=expected)
    assert oracle_reads
    assert summary["oraclePassed"] is False
    evaluation = json.loads(original_read(output / "evaluation.json", encoding="utf-8"))
    assert evaluation["B"]["passed"] is False
    assert evaluation["C"]["passed"] is True


def test_run_rejects_oracle_with_different_executed_inputs(oracle, tmp_path):
    small = deepcopy(oracle)
    small["cases"] = small["cases"][:1]
    inputs, expected, output = tmp_path / "input.json", tmp_path / "oracle.json", tmp_path / "out"
    write_document(inputs, load_document(small).to_dict())
    small["cases"][0]["nodes"][0]["name"] = "different input name"
    write_document(expected, small)
    with pytest.raises(ValueError, match="oracle inputs differ"):
        runner.run(inputs, output, oracle_path=expected)
    assert (output / "actual-B.json").is_file() and (output / "actual-C.json").is_file()
    assert not (output / "evaluation.json").exists()
