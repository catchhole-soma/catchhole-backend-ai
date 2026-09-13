"""Saved synthetic predictions pin upstream scoring independently of GH180 integration."""

import asyncio
import json
from pathlib import Path

import pytest

from evals.multi_stage_setting.evaluator import evaluate_multi_stage
from evals.multi_stage_setting.loaders import (
    load_gold_snapshot_v3,
    load_prediction_bundle_v3,
)


FIXTURES = Path(__file__).parent / "fixtures" / "multi_stage_setting_saved_predictions"


@pytest.mark.parametrize("mode", ["ORACLE", "FIXED", "ROLLING"])
def test_saved_predictions_keep_legacy_scoring_contract(mode: str) -> None:
    gold = load_gold_snapshot_v3(FIXTURES / "gold.json")
    predictions = load_prediction_bundle_v3(
        FIXTURES / f"{mode.lower()}-predictions.json",
        fixture_hash=gold.fixture_hash or "",
    )
    # Preserve the historical fixture. PR67 adds processing diagnostics; the new
    # baseline is captured independently from main without PR65.
    upstream = json.loads((FIXTURES / "expected-reports-main-05d9c2d.json").read_text())
    assert upstream["baselineCommit"] == "05d9c2d65498d28a1208b7f9bc8f0087307f64fc"
    expected = upstream["modes"][mode]["report"]

    actual = asyncio.run(evaluate_multi_stage(gold, predictions, semantic_judge=None))

    # Compare the entire JSON report, including denominators, per-episode states,
    # semantic-pending cases, harmful actions, and failures, rather than just accuracy.
    assert json.loads(json.dumps(actual)) == expected
