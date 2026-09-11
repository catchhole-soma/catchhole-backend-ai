import json
from pathlib import Path

from evals.character_status_stability.summarize import summarize_run


def _write(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _manifest(directory: Path, used_after: int | None) -> None:
    payload = {
        "phase": "validation", "requestedRuns": 10,
        "trials": [{"executionComplete": True}, {"executionComplete": False}],
        "semanticVerdict": "PENDING_INDEPENDENT_REVIEW", "models": {},
    }
    if used_after is not None:
        payload.update(usageBefore={"usedTokens": 0}, usageAfter={"usedTokens": used_after})
    _write(directory / "manifest.json", payload)


def _call(directory: Path, request_id: str, purpose: str, tokens: tuple[int, int, int]) -> None:
    _write(directory / "0001-spring-request.json", {
        "path": "/api/internal/v1/ai-token-usages/reserve",
        "body": {"requestId": request_id, "purpose": purpose},
    })
    _write(directory / "0002-spring-request.json", {
        "path": f"/api/internal/v1/ai-token-usages/{request_id}/settle",
        "body": {
            "inputTokens": tokens[0], "cachedInputTokens": tokens[1],
            "outputTokens": tokens[2], "outcome": "SUCCESS",
        },
    })


def test_summary_includes_nested_recomparison_and_counts_each_request_id_once(tmp_path) -> None:
    _manifest(tmp_path, 143)
    episode = tmp_path / "trial-01/episode-05"
    _call(episode / "worker", "extraction", "SETTING_EXTRACTION", (100, 80, 10))
    _call(episode / "recomparison/attempt-01/job-01/worker", "recompare", "CHARACTER_FACT_COMPARISON", (30, 20, 3))
    # Repeated ledger requests are idempotent and must not add provider usage twice.
    _call(episode / "recomparison/attempt-02/job-01/worker", "recompare", "CHARACTER_FACT_COMPARISON", (30, 20, 3))
    # Diagnostic calls outside the episode tree are accounted separately.
    _call(tmp_path / "probe", "diagnostic", "CHARACTER_FACT_COMPARISON", (999, 0, 1))

    result = summarize_run(tmp_path)

    assert result["settledTokens"] == 143  # Cached input is already part of input.
    assert result["ledgerMatches"] is True
    assert result["completedTrials"] == 1
    assert result["byPurpose"]["CHARACTER_FACT_COMPARISON"] == {
        "calls": 1, "inputTokens": 30, "cachedInputTokens": 20,
        "outputTokens": 3, "failedCalls": 0,
    }


def test_summary_preserves_interrupted_run_snapshot_mismatch(tmp_path) -> None:
    _manifest(tmp_path, 390930)
    _call(tmp_path / "trial-01/episode-05/worker", "complete", "SETTING_EXTRACTION", (380000, 100000, 10930))
    _call(tmp_path / "trial-02/episode-03/worker", "settled-before-stop", "SETTING_EXTRACTION", (120000, 50000, 7438))
    _write(tmp_path / "trial-02/episode-03/worker/0003-spring-request.json", {
        "path": "/api/internal/v1/ai-token-usages/reserve",
        "body": {"requestId": "usage-unknown", "purpose": "SETTING_EXTRACTION"},
    })

    result = summarize_run(tmp_path)

    assert result["settledTokens"] == 518368
    assert result["ledgerTokens"] == 390930
    assert result["ledgerMatches"] is False
    assert result["settledTokens"] - result["ledgerTokens"] == 127438
    assert result["byPurpose"]["SETTING_EXTRACTION"]["calls"] == 2


def test_summary_missing_ledger_is_unavailable_not_zero_balance(tmp_path) -> None:
    _manifest(tmp_path, None)
    result = summarize_run(tmp_path)
    assert result["settledTokens"] == 0
    assert result["ledgerTokens"] is None
    assert result["ledgerMatches"] is None
