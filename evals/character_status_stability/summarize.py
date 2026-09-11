"""Summarize recorded executions and settled token usage without novel-derived text."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from pathlib import Path


def summarize_run(directory: Path) -> dict:
    manifest = json.loads((directory / "manifest.json").read_text())
    reservations = {}
    settlements = {}
    request_paths = (
        path
        for episode in sorted(directory.glob("trial-*/episode-*"))
        for path in sorted(episode.rglob("*spring-request.json"))
    )
    for path in request_paths:
        request = json.loads(path.read_text())
        endpoint = request["path"]
        body = request.get("body") or {}
        if endpoint.endswith("/ai-token-usages/reserve"):
            reservations[body["requestId"]] = body
        elif "/ai-token-usages/" in endpoint and endpoint.endswith("/settle"):
            settlements[endpoint.split("/")[-2]] = body

    by_purpose = defaultdict(lambda: {
        "calls": 0, "inputTokens": 0, "cachedInputTokens": 0, "outputTokens": 0,
        "failedCalls": 0,
    })
    for request_id, usage in settlements.items():
        entry = by_purpose[reservations.get(request_id, {}).get("purpose", "UNKNOWN")]
        entry["calls"] += 1
        entry["failedCalls"] += usage.get("outcome") != "SUCCESS"
        for key in ("inputTokens", "cachedInputTokens", "outputTokens"):
            entry[key] += usage.get(key, 0)
    total = sum(item["inputTokens"] + item["outputTokens"] for item in by_purpose.values())
    before = manifest.get("usageBefore", {}).get("usedTokens")
    after = manifest.get("usageAfter", {}).get("usedTokens")
    ledger_delta = None if before is None or after is None else after - before
    return {
        "run": directory.name,
        "phase": manifest["phase"],
        "requestedTrials": manifest["requestedRuns"],
        "completedTrials": sum(item["executionComplete"] for item in manifest["trials"]),
        "semanticVerdict": manifest["semanticVerdict"],
        "models": manifest["models"],
        "settledTokens": total,
        "ledgerTokens": ledger_delta,
        "ledgerMatches": ledger_delta == total if ledger_delta is not None else None,
        "byPurpose": dict(by_purpose),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    directories = ([args.directory] if (args.directory / "manifest.json").exists()
                   else sorted(path.parent for path in args.directory.glob("*/manifest.json")))
    print(json.dumps([summarize_run(path) for path in directories], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
