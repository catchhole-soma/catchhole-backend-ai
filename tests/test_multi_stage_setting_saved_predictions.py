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
    # baseline is captured independently from PR65 after its merge of main.
    upstream = json.loads((FIXTURES / "expected-reports-pr65-6deac04.json").read_text())
    assert upstream["baselineCommit"] == "6deac0400c7b84a22715d669f30dfb12a7ca2b34"
    expected = upstream["modes"][mode]["report"]

    actual = asyncio.run(evaluate_multi_stage(gold, predictions, semantic_judge=None))

    # Compare the entire JSON report, including denominators, per-episode states,
    # semantic-pending cases, harmful actions, and failures, rather than just accuracy.
    assert json.loads(json.dumps(actual)) == expected
