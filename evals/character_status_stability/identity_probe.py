"""Replay recorded identity inputs; this is diagnostic, never an end-to-end trial."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID

from dotenv import dotenv_values

from app.analysis.character_identity_resolver import reconcile_episode_names
from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.schemas import CharacterSettingExtractionResult
from app.llm.openai_client import OpenAIResponsesClient
from evals.character_status_stability.runner import archive_run_code, revision, write_artifact


class RecordedClient:
    def __init__(self, delegate, directory: Path) -> None:
        self.delegate = delegate
        self.directory = directory
        self.calls = []

    async def create_text_response(self, **kwargs):
        record = {
            "model": kwargs["model"],
            "promptCacheKey": kwargs.get("prompt_cache_key"),
            "promptSha256": hashlib.sha256(
                (kwargs["system_prompt"] + "\0" + kwargs["user_prompt"]).encode(),
            ).hexdigest(),
            "usageUnknown": True,
        }
        self.calls.append(record)
        write_artifact(self.directory / f"call-{len(self.calls):02d}.json", record)
        try:
            result = await self.delegate.create_text_response(**kwargs)
        except Exception as exc:
            record.update(errorType=type(exc).__name__, usageUnknown=True)
            write_artifact(self.directory / f"call-{len(self.calls):02d}.json", record)
            raise
        record.update(
            response=result.text,
            inputTokens=result.input_token_count,
            cachedInputTokens=result.cached_input_token_count,
            outputTokens=result.output_token_count,
            usageUnknown=result.input_token_count is None or result.output_token_count is None,
        )
        write_artifact(self.directory / f"call-{len(self.calls):02d}.json", record)
        return result


async def run(args) -> None:
    source_episode = json.loads((args.episode_dir / "input.json").read_text())
    number = source_episode["episodeNo"]
    candidate_path, = args.episode_dir.glob("worker/*candidates-before-dedup.json")
    candidate_payload = json.loads(candidate_path.read_text())
    candidates = CharacterSettingExtractionResult.model_validate(
        {"candidates": candidate_payload["candidates"]},
    ).candidates
    claim = json.loads((args.episode_dir / "worker.json").read_text())["claim"]
    known = [KnownCharacter(character_id=UUID(row["characterId"]), name=row["name"])
             for row in claim["knownCharacters"]]
    current_path = args.source_dir / source_episode["filename"]
    current_text = current_path.read_bytes().decode("utf-8")
    previous_text = None
    if number > 1:
        previous_path, = args.source_dir.glob(f"{number - 1:03d}화 *.txt")
        previous_text = previous_path.read_bytes().decode("utf-8")[-14000:]
    key = dotenv_values(args.key_env_file).get("LLM_API_KEY")
    if not key:
        raise ValueError("LLM_API_KEY is required")
    output = Path("build/status-stability-probes") / datetime.now(UTC).strftime("identity-%Y%m%dT%H%M%SZ")
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    repo = Path(__file__).resolve().parents[2]
    archive_run_code(output, repo, args.java_repo)
    metadata = {
        "kind": "identity-diagnostic-only",
        "usageAccounting": "direct-provider-usage-not-Spring-ledger",
        "candidateArtifact": str(candidate_path),
        "candidateArtifactSha256": hashlib.sha256(candidate_path.read_bytes()).hexdigest(),
        "inputStage": "recorded-before-dedup-after-earlier-identity",
        "sourceSha256": hashlib.sha256(current_path.read_bytes()).hexdigest(),
        "ai": revision(repo),
        "model": "gpt-5.6-luna", "reasoningEffort": "none",
        "requestedRepeats": args.repeats, "attempts": [],
    }
    write_artifact(output / "manifest.json", metadata)
    provider = OpenAIResponsesClient(
        api_key=key, model="gpt-5.6-luna",
        responses_api_url="https://api.openai.com/v1/responses", reasoning_effort="none",
    )
    client = RecordedClient(provider, output)
    try:
        for index in range(1, args.repeats + 1):
            if revision(repo) != metadata["ai"]:
                raise RuntimeError("Product code changed during identity probe")
            item = {"repeat": index}
            try:
                result = await reconcile_episode_names(
                    llm_client=client, model="gpt-5.6-luna", max_output_tokens=6000,
                    context_episode_text=current_text, previous_episode_text=previous_text,
                    candidates=candidates, known_characters=known,
                )
                if len(result.candidates) != len(candidates) or any(
                    before.model_dump(exclude={"entity_name"}) != after.model_dump(exclude={"entity_name"})
                    for before, after in zip(candidates, result.candidates, strict=True)
                ):
                    raise AssertionError("Identity probe changed non-name candidate data")
                item.update(
                    executionComplete=True,
                    changed=[{"index": n, "before": before.entity_name, "after": after.entity_name}
                             for n, (before, after) in enumerate(zip(candidates, result.candidates, strict=True))
                             if before.entity_name != after.entity_name],
                    namesAfter=list(dict.fromkeys(row.entity_name for row in result.candidates)),
                )
            except Exception as exc:  # noqa: BLE001 - retain every failed diagnostic attempt.
                item.update(executionComplete=False, errorType=type(exc).__name__, error=str(exc)[:500])
            metadata["attempts"].append(item)
            write_artifact(output / "manifest.json", metadata)
            print(json.dumps({"output": str(output), **item}, ensure_ascii=False), flush=True)
            if client.calls and client.calls[-1].get("usageUnknown"):
                break
    finally:
        await provider.aclose()
        metadata["usage"] = {
            field: sum(call.get(field) or 0 for call in client.calls)
            for field in ("inputTokens", "cachedInputTokens", "outputTokens")
        }
        metadata["usage"]["unknownCalls"] = sum(bool(call.get("usageUnknown")) for call in client.calls)
        write_artifact(output / "manifest.json", metadata)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--episode-dir", required=True, type=Path)
    parser.add_argument("--source-dir", required=True, type=Path)
    parser.add_argument("--key-env-file", required=True, type=Path)
    parser.add_argument("--java-repo", required=True, type=Path)
    parser.add_argument("--repeats", type=int, choices=range(1, 11), default=3)
    asyncio.run(run(parser.parse_args()))
