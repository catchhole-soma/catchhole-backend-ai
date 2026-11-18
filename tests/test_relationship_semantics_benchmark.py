"""Exercise the actual subprocess entry point; avoid timing thresholds in CI."""

from pathlib import Path

import pytest

from scripts.relationship_semantics_experiment.benchmark import benchmark
from scripts.relationship_semantics_experiment.input import load_document

FIXTURE = Path(__file__).parent / "fixtures/relationship_semantics_cases.json"


def test_fresh_subprocess_benchmark_preserves_both_outputs():
    result = benchmark(FIXTURE, processes=1, iterations=1)
    b, c = result["runs"]["B"][0], result["runs"]["C"][0]
    assert b["queryCount"] == c["queryCount"] == 11
    assert b["outputSha256"] == c["outputSha256"]
    assert c["tripleCount"] > 0
    assert b["processPeakRssBytes"] > 0 and c["processPeakRssBytes"] > 0
    assert result["metadata"]["sourceTreeSha256"]


def test_empty_experiment_is_not_a_successful_comparison():
    with pytest.raises(ValueError):
        load_document({"schemaVersion": 1, "synthetic": True, "cases": []})
    with pytest.raises(ValueError, match="positive"):
        benchmark(FIXTURE, processes=0)
