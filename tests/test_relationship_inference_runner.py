"""Evidence boundaries and benchmark checks, independent of model internals."""

import copy
import json
from pathlib import Path

import pytest

from scripts.relationship_inference_experiment import benchmark, run
from scripts.relationship_inference_experiment.evidence import counts, digest, save
from scripts.relationship_inference_experiment.input import parse_cases
from scripts.relationship_inference_experiment.policy import prepare

FIXTURE = Path(__file__).parent / "fixtures" / "relationship_ontology_cases.json"


def family_document():
    return json.loads(FIXTURE.read_text(encoding="utf-8"))


def test_oracle_is_opened_after_both_actual_files(tmp_path, monkeypatch):
    oracle = tmp_path / "oracle.json"
    save(oracle, family_document())
    destination = tmp_path / "run"
    original = Path.read_text
    reads = []

    def observe(path, *args, **kwargs):
        if path == oracle:
            assert (destination / "actual-B.json").is_file()
            assert (destination / "actual-C.json").is_file()
            reads.append(path)
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", observe)
    result = run.execute(FIXTURE, oracle, destination)
    assert len(reads) == 1
    assert result["sameOutputs"]
    assert all(row["passedCases"] == 24 for row in result["evaluations"].values())
    assert "expected" not in (destination / "inputs.json").read_text()
    assert result["outputHashes"]["B"] == result["outputHashes"]["C"]


def test_oracle_for_different_input_cannot_score_success(tmp_path):
    document = family_document()
    document["cases"][0]["snapshotRevision"] += 1
    oracle = tmp_path / "oracle.json"
    save(oracle, document)
    output = tmp_path / "run"
    with pytest.raises(ValueError, match="inputs differ"):
        run.execute(FIXTURE, oracle, output)
    assert (output / "actual-B.json").exists()
    assert not (output / "evaluation.json").exists()


def test_evidence_is_never_overwritten(tmp_path):
    with pytest.raises(FileExistsError):
        run.execute(FIXTURE, FIXTURE, tmp_path)
    with pytest.raises(FileExistsError):
        benchmark.benchmark([FIXTURE], tmp_path)


def test_persisted_actual_tampering_cannot_be_scored_as_success(tmp_path, monkeypatch):
    original = run.save

    def corrupt(path, value):
        original(path, [] if path.name == "actual-C.json" else value)

    monkeypatch.setattr(run, "save", corrupt)
    destination = tmp_path / "run"
    with pytest.raises(ValueError, match="persisted actuals"):
        run.execute(FIXTURE, FIXTURE, destination)
    assert not (destination / "evaluation.json").exists()
    assert not (destination / "manifest.json").exists()


@pytest.mark.parametrize("count", [2, 100, 1000])
def test_scale_is_one_context_with_expected_direct_fact_count(count):
    cases = parse_cases(benchmark.scale_document(count))
    assert len(cases) == 1
    prepared = prepare(cases[0])
    assert len(prepared.eligible) == count
    assert len({row["temporalContextId"] for row in prepared.eligible}) == 1
    assert len(cases[0]["entities"]) == count // 2 * 3


@pytest.mark.parametrize("count", [0, 1, 3, -2])
def test_scale_rejects_invalid_size(count):
    with pytest.raises(ValueError):
        benchmark.scale_document(count)


def test_same_process_recomputation_discards_old_proof_and_matches_other_engine():
    from scripts.relationship_inference_experiment import owl_engine, python_engine

    document = benchmark.scale_document(4)
    original = copy.deepcopy(document)
    initial = parse_cases(document)
    changed = parse_cases(benchmark.withdraw_first_support(initial))
    results = []
    for engine in (python_engine, owl_engine):
        before = engine.run(prepare(initial[0]))
        after = engine.run(prepare(changed[0]))
        restored = engine.run(prepare(initial[0]))
        assert counts(before) == {
            "direct": 4,
            "derived": 6,
            "proofs": 6,
            "newRelations": 2,
            "withheld": 0,
            "excludedDerived": 0,
        }
        assert counts(after) == {
            "direct": 3,
            "derived": 4,
            "proofs": 4,
            "newRelations": 1,
            "withheld": 1,
            "excludedDerived": 0,
        }
        assert before == restored
        for row in after["derivedRelations"]:
            assert all("a-0-0" not in proof["premiseAssertionIds"] for proof in row["proofs"])
        results.append((before, after))
    assert results[0] == results[1]
    assert document == original


def test_independent_fresh_process_worker_smoke(tmp_path):
    path = tmp_path / "input.json"
    save(path, benchmark.scale_document(4))
    b = benchmark._invoke(path, "B", False, False, 30)
    c = benchmark._invoke(path, "C", False, False, 30)
    for phase in ("initial", "fullRecompute"):
        assert b[phase]["outputHash"] == c[phase]["outputHash"]
        assert b[phase]["times"]["totalMs"] > 0
    assert b["peakPythonBytes"] is None
    traced = benchmark._invoke(path, "B", False, True, 30)
    assert traced["peakPythonBytes"] > 0
    assert traced["initial"]["outputHash"] == b["initial"]["outputHash"]


def test_runner_timing_does_not_change_output():
    document = family_document()
    inputs, outputs, measurements = run.compute(document)
    assert len(measurements["engines"]["B"]) == 24
    assert inputs["cases"][0]["caseId"] == outputs["B"][0]["caseId"]
    assert digest(outputs["B"]) == digest(outputs["C"])
    assert all(row["proofMs"] >= 0 for row in measurements["engines"]["C"])
