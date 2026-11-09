"""User-authorized, isolated real-model A/B replay. No production writes or token ledger calls."""

import argparse
import asyncio
import hashlib
import json
import logging
import os
import subprocess
import sys
import time
from pathlib import Path
from uuid import UUID

WT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(WT))
from app.analysis.book_character_selection import BOOK_CHARACTER_SELECTION_CACHE_KEY
from app.analysis.character_name_resolver import ActiveCharacterStatus, KnownCharacter
from app.analysis.ordered_context import OrderedExtractionContext, ensure_ordered_prompt_fits
from app.analysis.semantic_current_context import SemanticCurrentInput
from app.analysis.setting_extractor import CharacterSettingExtractor, CharacterSettingSchemaHint
from app.llm.openai_client import OpenAIResponsesClient
from app.schemas.analysis_context import WorkerAnalysisContext
from app.schemas.worker import WorkerAnalysisKnownCharacterPayload, WorkerAnalysisSettingBookPayload
from scripts.measure_setting_book_context import BASELINE_MODULES, baseline_class

# Files this evaluation is allowed to differ in from the baseline; anything else means the
# baseline extractor would silently import changed dependencies.
EVALUATED_FILES = (
    *BASELINE_MODULES,
    "app/analysis/book_character_selection.py",
    "app/llm/prompts/book_character_selection.md",
    "app/core/config.py",
)


def call_stage(prompt_cache_key):
    return "selection" if prompt_cache_key.startswith(BOOK_CHARACTER_SELECTION_CACHE_KEY) else "extraction"


def parse_selection(raw_text):
    """Counts and refs of a stage-1 response; mentions stay in the private record only."""
    try:
        data = json.loads(raw_text)
    except (TypeError, ValueError):
        return None
    refs = {item["ref"] for item in data.get("selected", [])}
    ambiguous = {ref for item in data.get("ambiguous", []) for ref in item.get("candidate_refs", [])}
    return {"selected_refs": sorted(refs), "ambiguous_refs": sorted(ambiguous - refs),
            "unmatched_mentions": list(data.get("unmatched_mentions", []))}


def counterfactual_class(base):
    class CounterfactualBaseline(base):
        async def _extract_once(self, *args, **kwargs):
            # ONLY this isolated evaluation class bypasses the old application guard.
            # Original request text/options and model validation are unchanged.
            kwargs["requires_ordered_bound"] = False
            return await super()._extract_once(*args, **kwargs)

    return CounterfactualBaseline


class Recorder:
    def __init__(self, client):
        self.client = client
        self.calls = []

    async def create_text_response(self, **kwargs):
        start = time.monotonic()
        marker = "book_character_context:\n"
        selected_characters = None
        if marker in kwargs["user_prompt"]:
            context = json.JSONDecoder().raw_decode(kwargs["user_prompt"].split(marker, 1)[1])[0]
            selected_characters = len(context["characters"])
        record = {
            "stage": call_stage(kwargs.get("prompt_cache_key") or ""),
            "selected_characters": selected_characters,
            "model": kwargs["model"],
            "max_output_tokens": kwargs["max_output_tokens"],
            "prompt_sha256": hashlib.sha256(
                (kwargs["system_prompt"] + kwargs["user_prompt"]).encode()
            ).hexdigest(),
            "guard_tokens": ensure_ordered_prompt_fits(
                kwargs["system_prompt"],
                kwargs["user_prompt"],
                10**12,
                response_schema=kwargs["response_schema"],
            ),
        }
        self.calls.append(record)
        try:
            response = await self.client.create_text_response(**kwargs)
            record.update(
                input_tokens=response.input_token_count,
                cached_input_tokens=response.cached_input_token_count,
                output_tokens=response.output_token_count,
                raw_text=response.text,
                response_id=response.raw_response.get("id"),
                provider_model=response.raw_response.get("model"),
                status="SUCCEEDED",
            )
            return response
        except Exception as e:
            record.update(
                status="FAILED",
                error_type=type(e).__name__,
                http_status=getattr(getattr(e, "response", None), "status_code", None),
            )
            for key in ["input_token_count", "cached_input_token_count", "output_token_count"]:
                record[key.replace("_token_count", "_tokens")] = getattr(e, key, None)
            raise
        finally:
            record["seconds"] = round(time.monotonic() - start, 3)


def kwargs(case):
    known = tuple(WorkerAnalysisKnownCharacterPayload.model_validate(k) for k in case["known"])
    ordered = OrderedExtractionContext(
        WorkerAnalysisContext.model_validate(case["context"]), known, ()
    )
    return {
        "source_chunk_id": UUID(case["source_chunk_id"]),
        "chunk_text": case["body"],
        "schema_hints": tuple(CharacterSettingSchemaHint(**h) for h in case["schema_hints"]),
        "known_characters": tuple(
            KnownCharacter(
                k.character_id,
                k.name,
                tuple(ActiveCharacterStatus(s.fact_key, s.fact_value) for s in k.active_statuses),
            )
            for k in known
        ),
        "ordered_context": ordered,
        "semantic_current_context": SemanticCurrentInput(
            case["semantic"]["characters"], case["semantic"]["worlds"]
        ),
        "setting_book": WorkerAnalysisSettingBookPayload.model_validate(case["book"]),
    }


def write(path, data):
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n")
    os.chmod(path, 0o600)


def usage_total(calls, field):
    values = [call.get(field) for call in calls]
    return None if any(value is None for value in values) else sum(values)


async def main(args):
    logging.disable(logging.CRITICAL)
    cases = json.loads(args.inputs.read_text())
    baseline_sha, base = await asyncio.to_thread(baseline_class, args.baseline_ref)
    # The baseline keeps its own application guard unless the caller explicitly asks for the
    # counterfactual used when the baseline failed before any model call (pre-#148 history).
    before = counterfactual_class(base) if args.bypass_baseline_guard else base
    changed = (
        await asyncio.to_thread(
            subprocess.check_output,
            ["git", "diff", "--name-only", baseline_sha, "--", "app"],
            text=True,
        )
    ).splitlines()
    if set(changed) - set(EVALUATED_FILES):
        raise ValueError("Baseline dependencies differ; restore a matching evaluation checkout")
    head = (
        await asyncio.to_thread(subprocess.check_output, ["git", "rev-parse", "HEAD"], text=True)
    ).strip()
    code_sha = hashlib.sha256(
        b"".join(
            (WT / p).read_bytes()
            for p in (*EVALUATED_FILES, "app/llm/prompts/character_setting_extraction.md")
        )
    ).hexdigest()
    args.output_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(args.output_dir, 0o700)
    client = OpenAIResponsesClient(
        api_key=os.environ["LLM_API_KEY"],
        model=args.model,
        responses_api_url=os.environ.get("OPENAI_RESPONSES_API_URL")
        or "https://api.openai.com/v1/responses",
        reasoning_effort="medium",
    )
    sem = asyncio.Semaphore(2)
    results = []

    async def one(case, variant, repeat):
        path = args.output_dir / f"{case['case']}-{variant}-{repeat}.json"
        identity = {
            "input_snapshot_sha256": hashlib.sha256(
                json.dumps(case, ensure_ascii=False, sort_keys=True).encode()
            ).hexdigest(),
            "model": args.model,
            "reasoning_effort": "medium",
            "baseline_sha": baseline_sha,
            "code_sha256": code_sha,
        }
        if path.exists():
            record = json.loads(path.read_text())
            if any(record.get(k) != v for k, v in identity.items()):
                raise ValueError(
                    "Existing result has different input/model/code; use a new output directory"
                )
            results.append(record)
            print(f"REUSED {case['case']} {variant} {repeat} {record['status']}", flush=True)
            return
        async with sem:
            recorder = Recorder(client)
            cls = before if variant == "before" else CharacterSettingExtractor
            ext = cls(
                llm_client=recorder,
                model=args.model,
                max_attempts=3,
                max_output_tokens=32000,
                truncation_retry_max_output_tokens=64000,
            )
            record = {
                "case": case["case"],
                "variant": variant,
                "repeat": repeat,
                "model": args.model,
                "reasoning_effort": "medium",
                "baseline_guard_bypassed_for_semantic_comparison": variant == "before" and args.bypass_baseline_guard,
                "baseline_sha": baseline_sha,
                "head_sha": head,
                "code_sha256": code_sha,
                "source_sha256": hashlib.sha256(case["body"].encode()).hexdigest(),
                "input_snapshot_sha256": hashlib.sha256(
                    json.dumps(case, ensure_ascii=False, sort_keys=True).encode()
                ).hexdigest(),
            }
            started = time.monotonic()
            print(f"START {case['case']} {variant} {repeat}", flush=True)
            try:
                result = await ext.extract_from_chunk(**kwargs(case))
                record.update(status="SUCCEEDED", result=result.model_dump(mode="json"))
            except Exception as e:  # noqa: BLE001 -- preserve a sanitized failure, then exit nonzero
                record.update(status="FAILED", error_type=type(e).__name__)
            finally:
                selection_calls = [c for c in recorder.calls if c["stage"] == "selection"]
                record.update(seconds=round(time.monotonic() - started, 3), calls=recorder.calls,
                              catalogue_size=len(case["known"]),
                              selection=parse_selection(selection_calls[-1]["raw_text"])
                              if selection_calls and selection_calls[-1].get("status") == "SUCCEEDED" else None)
                write(path, record)
                results.append(record)
                print(
                    f"DONE {case['case']} {variant} {repeat} {record['status']} candidates={len(record.get('result', {}).get('candidates', []))} calls={len(recorder.calls)} seconds={record['seconds']}",
                    flush=True,
                )

    try:
        await asyncio.gather(
            *(
                one(case, variant, repeat)
                for repeat in range(1, args.repeats + 1)
                for case in cases
                for variant in args.variants
            )
        )
    finally:
        await client.http_client.aclose()
        write(args.output_dir / "results.json", results)
    summary = [
        {k: r[k] for k in ("case", "variant", "repeat", "status", "seconds")}
        | {
            "candidates": len(r.get("result", {}).get("candidates", [])),
            "calls": len(r["calls"]),
            "input_tokens": usage_total(r["calls"], "input_tokens"),
            "cached_input_tokens": usage_total(r["calls"], "cached_input_tokens"),
            "output_tokens": usage_total(r["calls"], "output_tokens"),
            "catalogue_size": r.get("catalogue_size"),
            "selected_characters": (len(r["selection"]["selected_refs"]) + len(r["selection"]["ambiguous_refs"]))
            if r.get("selection") else None,
            "stages": {
                stage: {
                    "calls": len(calls),
                    "input_tokens": usage_total(calls, "input_tokens"),
                    "cached_input_tokens": usage_total(calls, "cached_input_tokens"),
                    "output_tokens": usage_total(calls, "output_tokens"),
                    "guard_tokens_max": max((c["guard_tokens"] for c in calls), default=0),
                    "seconds": round(sum(c.get("seconds", 0) for c in calls), 3),
                }
                for stage in ("selection", "extraction")
                if (calls := [c for c in r["calls"] if c["stage"] == stage])
            },
        }
        for r in results
    ]
    write(args.output_dir / "usage.json", summary)
    if any(r["status"] != "SUCCEEDED" for r in results):
        raise SystemExit(1)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--confirm-paid", action="store_true")
    p.add_argument(
        "--inputs",
        type=Path,
        required=True,
        help="Private frozen input list; never commit customer snapshots",
    )
    p.add_argument(
        "--output-dir", type=Path, required=True, help="Private directory for full model responses"
    )
    p.add_argument("--baseline-ref", required=True)
    p.add_argument("--model", default="gpt-6.1-terra")
    p.add_argument("--repeats", type=int, default=1)
    p.add_argument(
        "--variants", nargs="+", choices=("before", "after"), default=["before", "after"]
    )
    p.add_argument(
        "--bypass-baseline-guard", action="store_true",
        help="Only for baselines that failed the input guard before any model call (pre-#148)",
    )
    args = p.parse_args()
    if not args.confirm_paid:
        p.error("Explicit --confirm-paid is required")
    if args.repeats < 1:
        p.error("--repeats must be positive")
    if not os.environ.get("LLM_API_KEY"):
        p.error("LLM_API_KEY is required")
    args.inputs = args.inputs.resolve()
    args.output_dir = args.output_dir.resolve()
    os.chdir(WT)
    asyncio.run(main(args))
