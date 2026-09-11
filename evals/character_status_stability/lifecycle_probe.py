"""Replay recorded STATUS decisions through lifecycle review; never an API e2e trial."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import secrets
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
from dotenv import dotenv_values

from app.analysis.character_fact_comparison_schemas import CharacterFactComparisonBatchDecision
from app.analysis.character_fact_projection import (
    CharacterProjectionEntry,
    CharacterProjectionState,
)
from app.analysis.character_status_lifecycle import reconcile_status_lifecycle
from app.analysis.json_response import safe_validation_error_summary
from app.llm.openai_client import OpenAIResponsesClient
from app.schemas.worker import WorkerCharacterFactComparisonBatchContextResponse
from evals.character_status_stability.runner import archive_run_code, revision, write_artifact


@dataclass
class RecordedBatch:
    context: WorkerCharacterFactComparisonBatchContextResponse
    decisions: list[CharacterFactComparisonBatchDecision]
    context_path: Path
    complete_path: Path

    def initial_entries(self) -> list[CharacterProjectionEntry]:
        return [
            CharacterProjectionEntry(
                reference=row.snapshot_ref,
                fact_type=row.fact_type,
                fact_key=row.fact_key,
                fact_value=row.fact_value,
                value_json=row.value_json,
                origin=row.origin,
                source_candidate_ref=row.source_candidate_ref,
                dependency_candidate_refs=tuple(row.dependency_candidate_refs),
            )
            for row in self.context.snapshot_entries
        ]


def load_recorded_batches(episode_dir: Path, batch_id: str | None = None) -> list[RecordedBatch]:
    """Use only matching, successful context/complete artifacts; reject partial batches."""
    artifacts = [
        (p, json.loads(p.read_text())) for p in sorted((episode_dir / "worker").glob("*.json"))
    ]
    result = []
    for index, (path, data) in enumerate(artifacts):
        route = data.get("path", "")
        if not (
            path.name.endswith("spring-request.json")
            and "character-fact-comparison-batches/" in route
            and route.endswith("/complete")
        ):
            continue
        if batch_id is not None and route.split("/")[-2] != batch_id:
            continue
        body = data["body"]
        matches = [
            (p, d["body"]["data"])
            for p, d in artifacts[:index]
            if p.name.endswith("spring-response.json")
            and d.get("status") == 200
            and d.get("path") == route.removesuffix("/complete") + "/context"
            and (d.get("body", {}).get("data") or {}).get("contextToken")
            == body.get("contextToken")
        ]
        if not matches:
            raise ValueError("Matching recorded context is missing")
        context_path, payload = matches[-1]
        if payload["canonicalFactType"] != "STATUS":
            continue
        following = next(
            (
                d
                for p, d in artifacts[index + 1 :]
                if p.name.endswith("spring-response.json") and d.get("path") == route
            ),
            None,
        )
        if (
            following is None
            or following.get("status") != 200
            or not following.get("body", {}).get("success")
        ):
            continue
        if body.get("failures"):
            raise ValueError("Recorded STATUS batch contains typed failures")
        context = WorkerCharacterFactComparisonBatchContextResponse.model_validate(payload)
        decisions = [
            CharacterFactComparisonBatchDecision.model_validate(
                {
                    "candidate_ref": d["candidateRef"],
                    "resolved_canonical_fact_key": d["resolvedCanonicalFactKey"],
                    "operation": d["operation"],
                    "target_ref": d.get("targetSnapshotRef"),
                    "removed_snapshot_refs": d.get("removedSnapshotRefs", []),
                    "proposed_fact_value": d.get("proposedFactValue"),
                    "proposed_value_json": d.get("proposedValueJson"),
                    "temporal_scope": d["temporalScope"],
                    "comparison_reason": d["comparisonReason"],
                }
            )
            for d in body["decisions"]
        ]
        by_ref = {d.candidate_ref: d for d in decisions}
        if len(by_ref) != len(decisions) or set(by_ref) != {
            c.candidate_ref for c in context.candidates
        }:
            raise ValueError("Recorded candidate/decision coverage differs")
        result.append(
            RecordedBatch(
                context, [by_ref[c.candidate_ref] for c in context.candidates], context_path, path
            )
        )
    if not result:
        raise ValueError("No successful recorded STATUS batch found")
    return result


class RecordedClient:
    """Private request/body audit without headers, credentials, or exception text."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory
        self.delegate = None
        self.calls: list[dict] = []
        self.run_number = 0
        self.batch_id = ""

    def save(self) -> None:
        write_artifact(self.directory / f"call-{len(self.calls):04d}.json", self.calls[-1])

    async def record_request(self, request: httpx.Request) -> None:
        payload = json.loads(request.content)
        if payload.get("store") is not False:
            raise ValueError("Probe requires store=false")
        self.calls[-1]["request"] = payload
        self.save()

    async def record_response(self, response: httpx.Response) -> None:
        await response.aread()
        await self.record_request(response.request)
        try:
            raw = response.json()
        except ValueError:
            raw = response.text
        row = self.calls[-1]
        row.update(statusCode=response.status_code, rawResponse=raw)
        usage = raw.get("usage") if isinstance(raw, dict) else None
        usage = usage if isinstance(usage, dict) else {}
        details = usage.get("input_tokens_details")
        details = details if isinstance(details, dict) else {}
        raw_counts = (
            usage.get("input_tokens"),
            details.get("cached_tokens"),
            usage.get("output_tokens"),
        )
        row.update(
            {
                key: value if type(value) is int and value >= 0 else None
                for key, value in zip(TOKEN_FIELDS, raw_counts, strict=True)
            }
        )
        row["usageUnknown"] = any(row.get(k) is None for k in TOKEN_FIELDS)
        self.save()

    async def create_text_response(self, **kwargs):
        self.calls.append(
            {
                "run": self.run_number,
                "batchId": self.batch_id,
                "model": kwargs.get("model"),
                "promptCacheKey": kwargs.get("prompt_cache_key"),
                "promptSha256": hashlib.sha256(
                    (kwargs["system_prompt"] + "\0" + kwargs["user_prompt"]).encode()
                ).hexdigest(),
                "usageUnknown": True,
            }
        )
        self.save()
        try:
            result = await self.delegate.create_text_response(**kwargs)
        except Exception as exc:
            self.calls[-1]["errorType"] = type(exc).__name__
            self.calls[-1]["errorSummary"] = safe_validation_error_summary(exc)
            self.save()
            raise
        self.calls[-1].update(responseText=result.text)
        self.save()
        return result


TOKEN_FIELDS = ("inputTokens", "cachedInputTokens", "outputTokens")


def replay(batch: RecordedBatch, decisions: list[CharacterFactComparisonBatchDecision]) -> dict:
    state = CharacterProjectionState(batch.initial_entries())
    applications = []
    for candidate, decision in zip(batch.context.candidates, decisions, strict=True):
        application = state.apply(
            candidate_ref=candidate.candidate_ref,
            projected_snapshot_ref=candidate.projected_snapshot_ref,
            fact_type="STATUS",
            resolved_fact_key=decision.resolved_canonical_fact_key,
            value_type=candidate.value_type,
            candidate_value_json=candidate.value_json,
            decision=decision,
        )
        applications.append(
            {
                "candidateRef": candidate.candidate_ref,
                "dependencyCandidateRefs": application.dependency_candidate_refs,
            }
        )
    return {
        "decisions": [d.model_dump(mode="json") for d in decisions],
        "applications": applications,
        "remainingStatuses": [asdict(e) for e in state.entries],
    }


async def run(args: argparse.Namespace) -> Path:
    batches = load_recorded_batches(args.episode_dir, args.batch_id)
    original_replays = [replay(batch, batch.decisions) for batch in batches]
    key = dotenv_values(args.key_env_file).get("LLM_API_KEY")
    if not key:
        raise ValueError("LLM_API_KEY is required")
    repo = Path(__file__).resolve().parents[2]
    output = args.output_dir / (
        datetime.now(UTC).strftime("lifecycle-%Y%m%dT%H%M%SZ-") + secrets.token_hex(3)
    )
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    code = {"ai": revision(repo), "java": revision(args.java_repo)}
    archive_run_code(output, repo, args.java_repo)
    metadata = {
        "kind": "status-lifecycle-diagnostic-only",
        "usageAccounting": "direct-provider-probe-not-Spring-ledger",
        "notEndToEnd": True,
        **code,
        "codeArchiveSha256": hashlib.sha256((output / "code.tar.gz").read_bytes()).hexdigest(),
        "probeSourceSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "model": args.model,
        "reasoningEffort": args.reasoning_effort,
        "store": False,
        "requestedRuns": args.runs,
        "maxAttempts": args.max_attempts,
        "maxOutputTokens": args.max_output_tokens,
        "maxInputTokens": args.max_input_tokens,
        "recordedInputs": [],
        "attempts": [],
    }
    for batch, original in zip(batches, original_replays, strict=True):
        batch_id = str(batch.context.comparison_batch_id)
        metadata["recordedInputs"].append(
            {
                "batchId": batch_id,
                "artifacts": [
                    {"path": str(p), "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}
                    for p in [batch.context_path, batch.complete_path]
                ],
            }
        )
        write_artifact(
            output / f"input-{batch_id}.json",
            {
                "context": batch.context.model_dump(mode="json"),
                "originalReplay": original,
            },
        )
    write_artifact(output / "manifest.json", metadata)
    recorded = RecordedClient(output)
    http_client = httpx.AsyncClient(
        timeout=120,
        event_hooks={"request": [recorded.record_request], "response": [recorded.record_response]},
    )
    provider = OpenAIResponsesClient(
        api_key=key,
        model=args.model,
        responses_api_url="https://api.openai.com/v1/responses",
        reasoning_effort=args.reasoning_effort,
        http_client=http_client,
    )
    recorded.delegate = provider
    try:
        for number in range(1, args.runs + 1):
            if revision(repo) != code["ai"] or revision(args.java_repo) != code["java"]:
                raise RuntimeError("Product code changed during lifecycle probe")
            for batch in batches:
                recorded.run_number = number
                recorded.batch_id = str(batch.context.comparison_batch_id)
                item = {
                    "run": number,
                    "batchId": recorded.batch_id,
                    "firstCall": len(recorded.calls) + 1,
                }
                try:
                    reviewed = await reconcile_status_lifecycle(
                        llm_client=recorded,
                        model=args.model,
                        max_output_tokens=args.max_output_tokens,
                        max_attempts=args.max_attempts,
                        max_input_tokens=args.max_input_tokens,
                        matched_character_name=batch.context.matched_character_name,
                        candidates=batch.context.candidates,
                        initial_entries=batch.initial_entries(),
                        decisions=batch.decisions,
                    )
                    if revision(repo) != code["ai"] or revision(args.java_repo) != code["java"]:
                        raise RuntimeError("Product code changed during lifecycle probe")
                    write_artifact(
                        output / f"result-{number:02d}-{recorded.batch_id}.json",
                        replay(batch, reviewed),
                    )
                    item["executionComplete"] = True
                except Exception as exc:  # noqa: BLE001 - retain every failed diagnostic attempt.
                    item.update(
                        executionComplete=False,
                        errorType=type(exc).__name__,
                        errorSummary=safe_validation_error_summary(exc),
                    )
                item["lastCall"] = len(recorded.calls)
                metadata["attempts"].append(item)
                write_artifact(output / "manifest.json", metadata)
                print(json.dumps({"output": str(output), **item}), flush=True)
                if any(c["usageUnknown"] for c in recorded.calls):
                    return output
    finally:
        await provider.aclose()
        metadata["usage"] = {
            field: sum(c.get(field) or 0 for c in recorded.calls) for field in TOKEN_FIELDS
        }
        metadata["usage"]["unknownCalls"] = sum(c["usageUnknown"] for c in recorded.calls)
        metadata["usage"]["cachedTokensIncludedInInput"] = True
        write_artifact(output / "manifest.json", metadata)
    return output


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--episode-dir", required=True, type=Path)
    result.add_argument("--java-repo", required=True, type=Path)
    result.add_argument("--key-env-file", required=True, type=Path)
    result.add_argument("--runs", type=int, choices=range(1, 11), default=1)
    result.add_argument("--batch-id")
    result.add_argument("--output-dir", type=Path, default=Path("build/status-stability-probes"))
    result.add_argument("--model", default="gpt-5.6-luna")
    result.add_argument("--reasoning-effort", default="none")
    result.add_argument("--max-attempts", type=int, choices=range(1, 6), default=3)
    result.add_argument("--max-output-tokens", type=int, default=16000)
    result.add_argument("--max-input-tokens", type=int, default=64000)
    return result


if __name__ == "__main__":
    asyncio.run(run(parser().parse_args()))
