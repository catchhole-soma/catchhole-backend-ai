"""Replay successive episodes through the local Java API and production AI worker.

Artifacts are evidence for an independent semantic review, not a model-generated
pass/fail declaration. No expected character state is sent to the worker.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import os
import secrets
import subprocess
import tarfile
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from dotenv import dotenv_values


def write_artifact(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, default=str) + "\n")
    path.chmod(0o600)


def source_manifest(source_dir: Path) -> list[dict]:
    result = []
    for number in range(1, 6):
        matches = sorted(source_dir.glob(f"{number:03d}화 *.txt"))
        if len(matches) != 1:
            raise ValueError(f"Expected exactly one episode {number} source file")
        path = matches[0]
        data = path.read_bytes()
        result.append({
            "episodeNo": number,
            "filename": path.name,
            "sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data),
            "characters": len(data.decode("utf-8")),
        })
    return result


def revision(repo: Path) -> dict:
    sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"], cwd=repo, text=True
    ).strip()
    digest = hashlib.sha256()
    source_root = repo / ("app" if (repo / "app").is_dir() else "src/main")
    for path in sorted(source_root.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts:
            digest.update(str(path.relative_to(repo)).encode())
            digest.update(b"\0")
            digest.update(path.read_bytes())
    return {"commit": sha, "productSourceSha256": digest.hexdigest()}


def harness_revision() -> str:
    digest = hashlib.sha256()
    for path in sorted(Path(__file__).parent.glob("*.py")):
        digest.update(path.name.encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def archive_run_code(output: Path, ai_repo: Path, java_repo: Path) -> None:
    """Retain the uncommitted product and harness used by this exact attempt."""
    path = output / "code.tar.gz"
    with tarfile.open(path, "w:gz") as archive:
        for repo, relative, label in (
            (ai_repo, "app", "ai"),
            (java_repo, "src/main", "java"),
            (ai_repo, "evals/character_status_stability", "ai"),
        ):
            for source in sorted((repo / relative).rglob("*")):
                if source.is_file() and "__pycache__" not in source.parts:
                    archive.add(source, arcname=f"{label}/{source.relative_to(repo)}")
    path.chmod(0o600)


def configure(args: argparse.Namespace):
    from app.core.config import get_settings

    if urlsplit(args.backend_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Evaluation backend must be localhost")
    if urlsplit(args.database_url).hostname not in {"localhost", "127.0.0.1", "::1"}:
        raise ValueError("Evaluation database must be localhost")
    key = os.environ.get("LLM_API_KEY")
    if not key and args.key_env_file:
        key = dotenv_values(args.key_env_file).get("LLM_API_KEY")
    if not key:
        raise ValueError("LLM_API_KEY is required (only this key is read from --key-env-file)")
    os.environ.update({
        "LLM_API_KEY": key,
        "LLM_MODEL": "gpt-5.6-terra",
        "LLM_EXTRACTION_MODEL": "gpt-5.6-terra",
        "LLM_SUBJECT_RESOLUTION_MODEL": "gpt-5.6-luna",
        "LLM_COMPARISON_MODEL": "gpt-5.6-luna",
        "LLM_REASONING_EFFORT": args.reasoning_effort,
        "OPENAI_RESPONSES_API_URL": "https://api.openai.com/v1/responses",
        "SPRING_INTERNAL_API_BASE_URL": args.backend_url,
        "SPRING_INTERNAL_API_KEY": os.environ.get(
            "STATUS_EVAL_INTERNAL_API_KEY", "local-development-internal-api-key"
        ),
        "DATABASE_URL": args.database_url,
        "TZ": "Asia/Seoul",
        "EMBEDDING_GENERATION_ENABLED": "false",
        "AI_WORKER_CONCURRENCY": "1",
        "LLM_MAX_CONCURRENT_REQUESTS": "1",
    })
    get_settings.cache_clear()
    return get_settings()


async def capture_state(api, work_id: str) -> list[dict]:
    characters = await api.get_characters(work_id)
    result = []
    for character in characters:
        character_id = character["id"]
        result.append({
            "character": character,
            "snapshot": await api.get_character_snapshot(work_id, character_id),
            "facts": await api.get_character_facts(work_id, character_id),
        })
    return result


def progress(**fields) -> None:
    print(json.dumps(fields, ensure_ascii=False, default=str), flush=True)


async def confirm_with_recomparison(
    *, api, work_id: str, batch_id: str, settings, episode_dir: Path,
    run_comparison_job=None,
) -> dict:
    """Model the user's refresh, wait for comparison, and confirm again flow.

    Only the API's explicit NOT_READY response permits another confirmation.
    Every attempt and real hidden Worker job is retained; semantic failures are
    never converted to a successful confirmation or retried as new extraction.
    """
    if run_comparison_job is None:
        from evals.character_status_stability.worker import run_one_comparison_job
        run_comparison_job = run_one_comparison_job
    confirmed, attempts = [], []
    for attempt_no in range(1, 4):
        attempt_dir = episode_dir / "recomparison" / f"attempt-{attempt_no:02d}"
        result = await api.confirm_pending_groups(work_id, batch_id)
        write_artifact(attempt_dir / "confirmation.json", result)
        confirmed.extend(result["confirmed"])
        errors = [row for row in result["unresolved"] if row.get("error") or row.get("code")]
        invalid_or_failed = any(
            diagnostic.get("comparisonStatus") == "FAILED"
            or diagnostic.get("reason") == "CANDIDATE_VALUE_INVALID"
            for row in result["unresolved"]
            for diagnostic in row.get("diagnostics", [])
        )
        should_recompare = bool(errors) and not invalid_or_failed and all(
            row.get("error", {}).get("code") == "SETTING_CANDIDATE_COMPARISON_NOT_READY"
            and row.get("error", {}).get("statusCode") == 409
            for row in errors
        )
        attempt = {"number": attempt_no, "recomparisonRequired": should_recompare, "jobs": []}
        attempts.append(attempt)
        merged = {**result, "confirmed": list(confirmed), "confirmationAttempts": attempts}
        write_artifact(episode_dir / "confirmation.json", merged)
        if not should_recompare or attempt_no == 3:
            return merged

        groups = await api.get_candidate_groups(work_id, batch_id, review_status="PENDING_REVIEW")
        candidate_ids = {
            row["id"] for group in groups for row in group.get("candidates", [])
            if row.get("candidateKind") == "SETTING"
        }
        if not candidate_ids:
            raise RuntimeError("Recomparison requested without pending setting candidates")
        # There can be one queued comparison job per candidate. Drain the isolated
        # comparison queue to its empty response before another group mutation.
        for job_no in range(1, len(candidate_ids) + 2):
            job_dir = attempt_dir / f"job-{job_no:02d}"
            outcome = await run_comparison_job(
                settings=settings, audit_dir=job_dir / "worker",
                expected_work_id=work_id, expected_candidate_ids=candidate_ids,
            )
            write_artifact(job_dir / "result.json", outcome)
            attempt["jobs"].append(outcome)
            write_artifact(episode_dir / "confirmation.json", merged)
            if not outcome.get("claimed"):
                if outcome.get("failureCode") != "NO_CLAIMABLE_JOB":
                    raise RuntimeError("Recomparison claim failed; see recorded Worker result")
                break
            if not outcome.get("success"):
                raise RuntimeError("Recomparison failed; see recorded Worker result")
        else:
            raise RuntimeError("Recomparison queue exceeded the recorded candidate count")
    raise AssertionError("Unreachable confirmation loop")


async def run(args: argparse.Namespace) -> dict:
    from evals.character_status_stability.api import EvalApi
    from evals.character_status_stability.worker import run_one_job

    sources = source_manifest(args.source_dir)
    settings = configure(args)
    repo = Path(__file__).resolve().parents[2]
    run_id = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ") + "-" + secrets.token_hex(3)
    output = args.output_dir / f"{args.phase}-{run_id}"
    output.mkdir(parents=True, exist_ok=False)
    output.chmod(0o700)
    metadata = {
        "runId": run_id,
        "phase": args.phase,
        "requestedRuns": args.runs,
        "flow": "analyze-confirm-next-episode",
        "reviewPolicy": "frontend-default-group-confirm-with-history-only-and-recomparison-v4",
        "ai": revision(repo),
        "java": revision(args.java_repo),
        "harnessSourceSha256": harness_revision(),
        "sources": sources,
        "models": {
            "extraction": settings.effective_llm_extraction_model,
            "subjectResolution": settings.effective_llm_subject_resolution_model,
            "comparison": settings.effective_llm_comparison_model,
            "reasoningEffort": settings.llm_reasoning_effort,
        },
        "parityExceptions": ["local file storage", "fake SMS", "empty world extraction"],
        "semanticVerdict": "PENDING_INDEPENDENT_REVIEW",
        "trials": [],
    }
    write_artifact(output / "manifest.json", metadata)
    archive_run_code(output, repo, args.java_repo)
    progress(event="evaluation_started", output=str(output), runs=args.runs)
    account_tag = secrets.token_hex(8)
    async with EvalApi(args.backend_url) as api:
        await api.signup_or_login(
            email=f"gh126-{account_tag}@example.invalid",
            password=f"Eval-{secrets.token_urlsafe(24)}-126",
            display_name="상태 평가",
            phone_number=f"010{secrets.randbelow(100000000):08d}",
        )
        metadata["usageBefore"] = await api.get_usage()
        for number in range(1, args.runs + 1):
            trial_dir = output / f"trial-{number:02d}"
            trial = {"number": number, "executionComplete": False, "episodes": []}
            metadata["trials"].append(trial)
            try:
                work = await api.create_work(f"GH126 {args.phase} {run_id} #{number}")
                work_id = str(work["id"])
                trial["workId"] = work_id
                for source in sources:
                    if (revision(repo) != metadata["ai"]
                            or revision(args.java_repo) != metadata["java"]
                            or harness_revision() != metadata["harnessSourceSha256"]):
                        raise RuntimeError("Evaluation code changed during the recorded run")
                    episode_no = source["episodeNo"]
                    episode_dir = trial_dir / f"episode-{episode_no:02d}"
                    progress(event="episode_started", trial=number, episode=episode_no)
                    before = await capture_state(api, work_id)
                    write_artifact(episode_dir / "before.json", before)
                    source_path = args.source_dir / source["filename"]
                    if hashlib.sha256(source_path.read_bytes()).hexdigest() != source["sha256"]:
                        raise RuntimeError("Source changed during evaluation")
                    upload = await api.upload_episode(
                        work_id, episode_no, source_path.read_bytes(),
                        source["filename"],
                    )
                    batch_id = str(upload["batchId"])
                    episode_id = str(upload["createdEpisodes"][0]["id"])
                    job = await api.create_analysis_job(work_id, batch_id, episode_id)
                    job_id = str(job["id"])
                    episode = {
                        "episodeNo": episode_no, "episodeId": episode_id,
                        "batchId": batch_id, "jobId": job_id,
                    }
                    trial["episodes"].append(episode)
                    write_artifact(episode_dir / "input.json", {**source, **episode})
                    outcome = await run_one_job(
                        settings=settings, storage_root=args.storage_root,
                        audit_dir=episode_dir / "worker",
                    )
                    write_artifact(episode_dir / "worker.json", outcome)
                    if str(outcome.get("analysisJobId")) != job_id:
                        raise RuntimeError("Worker claimed a different job; isolate the queue")
                    job_result = await api.get_job(work_id, job_id)
                    write_artifact(episode_dir / "job.json", job_result)
                    if not outcome.get("success") or job_result.get("status") != "SUCCEEDED":
                        raise RuntimeError("Analysis failed; see recorded job and worker results")
                    candidates = await api.get_candidate_groups(work_id, batch_id)
                    write_artifact(episode_dir / "candidates-before-confirm.json", candidates)
                    confirmation = await confirm_with_recomparison(
                        api=api, work_id=work_id, batch_id=batch_id, settings=settings,
                        episode_dir=episode_dir,
                    )
                    write_artifact(episode_dir / "confirmation.json", confirmation)
                    after = await capture_state(api, work_id)
                    write_artifact(episode_dir / "after.json", after)
                    episode["confirmationComplete"] = confirmation["complete"]
                    episode["characters"] = [{
                        "id": item["character"]["id"],
                        "name": item["character"]["name"],
                        "statuses": item["snapshot"].get("statuses", []),
                    } for item in after]
                    write_artifact(output / "manifest.json", metadata)
                    progress(event="episode_finished", trial=number, episode=episode_no,
                             confirmed=confirmation["complete"], characters=len(after))
                    # Leaving genuinely unknown people in review is a normal user action.
                    # Preserve the rows and diagnostics instead of inventing a match or
                    # pretending that an unconfirmed target transition passed evaluation.
                    if any(
                        item.get("code") or item.get("error")
                        for item in confirmation.get("unresolved", [])
                    ):
                        raise RuntimeError("Confirmation API failed; inspect unresolved diagnostics")
                trial["executionComplete"] = True
            except Exception as exc:  # noqa: BLE001 - persist every failed trial before stopping.
                trial["errorType"] = type(exc).__name__
                trial["error"] = str(exc)[:1000]
                write_artifact(output / "manifest.json", metadata)
                progress(event="trial_stopped", trial=number, error=type(exc).__name__)
                # A queued recompare could otherwise be mistaken for the next trial's job.
                break
            metadata["usageAfter"] = await api.get_usage()
            write_artifact(output / "manifest.json", metadata)
            if (output / "STOP_AFTER_TRIAL").exists():
                metadata["stoppedAfterTrial"] = number
                progress(event="operator_stop_after_trial", trial=number)
                break
        metadata["usageAfter"] = await api.get_usage()
    metadata["finishedAt"] = datetime.now(UTC).isoformat()
    write_artifact(output / "manifest.json", metadata)
    progress(event="evaluation_finished", output=str(output),
             complete=sum(t["executionComplete"] for t in metadata["trials"]),
             semanticVerdict=metadata["semanticVerdict"])
    return metadata


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-dir", type=Path, required=True)
    parser.add_argument("--java-repo", type=Path, required=True)
    parser.add_argument("--key-env-file", type=Path)
    parser.add_argument("--backend-url", default="http://127.0.0.1:18086")
    parser.add_argument("--database-url", default=(
        "postgresql+psycopg://status_eval:local-status-eval-only@127.0.0.1:25432/"
        "catchhole_status_eval"
    ))
    parser.add_argument("--storage-root", type=Path, default=Path("/tmp/catchhole-gh126-eval-storage"))
    parser.add_argument("--output-dir", type=Path, default=Path("build/status-stability"))
    parser.add_argument("--runs", type=int, choices=range(1, 11), default=1)
    parser.add_argument("--phase", choices=("baseline", "experiment", "safety", "validation"), required=True)
    parser.add_argument("--reasoning-effort", default="none", choices=("none", "low", "medium", "high"))
    args = parser.parse_args()
    logging.basicConfig(level=logging.WARNING)
    result = asyncio.run(run(args))
    if not all(t["executionComplete"] for t in result["trials"]):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
