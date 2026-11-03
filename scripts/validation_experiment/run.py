"""Validation experiment CLI (grid v4). Every paid step needs --confirm-paid; --fake uses an offline provider.

Steps, in order (all outputs under --out, which must be outside this repository):
  synthetic      write a small invented work (raw export + manuscripts) for dry runs and smoke tests
  build-fixture  baseline-raw.json + manuscripts -> fixture/fixture.json
  seed-db        disposable loopback validation_experiment_* database: Backend migrations, member, work, episodes
  index-rag      Option A: chunk and embed the prior episodes locally (paid embeddings, or --fake)
  plan           PLANNER once (model/effort as given) -> plan/
  judge          JUDGE over the saved plan (model/effort/label as given) -> judge/<label>/
  typo           TYPO (model/effort/label as given) -> typo/<label>/
  report         compare judge and typo runs -> report/
The API key is read from LLM_API_KEY only and never written. Experiment mode records question-level failures
and continues; budgets are the production configuration v2 values unless overridden.
"""

import argparse
import asyncio
import json
import logging
import sys
import time
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import httpx

from app.embeddings.client import OpenAIEmbeddingsClient
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.responses import LlmTextResponse
from app.validation.contracts import (
    BudgetState,
    IncompleteInspection,
    InvocationBudget,
    QuestionBatch,
)
from app.validation.execution import ValidationRoleClients
from app.validation.models import SCHEMA_VERSION, RoleRuntime
from app.validation.planning import SettingPlanner
from app.validation.projection import source_catalogue
from app.validation.service import SettingValidationService
from app.validation.typo import check_typos
from scripts.validation_experiment import fake_provider, synthetic
from scripts.validation_experiment.build_fixture import build, fixture_hash
from scripts.validation_experiment.common import (
    REPO_ROOT,
    HarnessError,
    api_key,
    file_sha256,
    git_identity,
    local_db_url,
    manuscript_path,
    outside_repo,
    read_json,
    require_paid,
    usd,
    utc_stamp,
    write_json,
)
from scripts.validation_experiment.fixture_client import FixtureDataClient
from scripts.validation_experiment.local_ledger import CallRecorder, LocalLedger

RESPONSES_URL = "https://api.openai.com/v1/responses"
EMBEDDINGS_URL = "https://api.openai.com/v1/embeddings"
LOGIC_VERSION = "v0045"
SETTING = {"model_calls": 200, "lookups": 300, "seconds": 3600.0, "input_tokens": 80000, "segment_chars": 10000,
           "context_chars": 1000, "search_limit": 10}
TYPO = {"model_calls": 20, "lookups": 20, "seconds": 900.0, "input_tokens": 20000, "segment_chars": 2000,
        "context_chars": 100, "search_limit": 10}
OUTPUT_TOKENS = {"PLANNER": 50000, "JUDGE": 20000, "TYPO": 12000}
LLM_CONCURRENCY = 4  # production LLM_MAX_CONCURRENT_REQUESTS for the validation Worker


def http_client(recorder, fake):
    hooks = {"request": [recorder.request], "response": [recorder.response]}
    if fake:
        return httpx.AsyncClient(transport=fake_provider.transport(), event_hooks=hooks)
    return httpx.AsyncClient(timeout=httpx.Timeout(120, read=900), event_hooks=hooks)


def spent_so_far(out):
    """USD already recorded by every ledger under the experiment root (the cumulative spend cap)."""
    total = 0.0
    for path in Path(out).rglob("ledger.jsonl"):
        for line in path.read_text(encoding="utf-8").splitlines():
            row = json.loads(line)
            total += usd(row["model"], row.get("inputTokens") or 0, row.get("outputTokens") or 0) or 0.0
    return total


class Runtime:
    """One step's provider clients, ledger and recorder; closed by `aclose`."""

    def __init__(self, args, step_dir, runtimes):
        self.fake = bool(args.fake)
        key = "fake-offline-key" if self.fake else api_key()
        cap = None
        if args.spend_cap_usd is not None:
            cap = max(0.0, args.spend_cap_usd - spent_so_far(args.out))
            if cap <= 0:
                raise HarnessError("Spend cap already reached by earlier steps")
        self.ledger = LocalLedger(step_dir / "ledger.jsonl", {role: runtime.call_options.reasoning_effort
                                                              for role, runtime in runtimes.items()},
                                  spend_cap_usd=cap, price=usd)
        self.recorder = CallRecorder(step_dir / "calls.jsonl")
        self.http = http_client(self.recorder, self.fake)
        self.raw = OpenAIResponsesClient(key, next(iter(runtimes.values())).model, RESPONSES_URL, http_client=self.http)
        self.embeddings = OpenAIEmbeddingsClient(key, "text-embedding-3-small", 1536, "v1", EMBEDDINGS_URL, self.http)
        self.semaphore = asyncio.Semaphore(LLM_CONCURRENCY)
        self.claim = SimpleNamespace(job_id=uuid4(), lease_token=uuid4())
        self.roles = ValidationRoleClients(self.raw, self.ledger, self.claim, runtimes, request_semaphore=self.semaphore)

    async def aclose(self):
        await self.http.aclose()


def runtime(model, effort, role, output_tokens=None):
    """Production configuration v2 output caps unless an experiment overrides them explicitly."""
    return RoleRuntime.of(model, effort, output_tokens or OUTPUT_TOKENS[role])


def step_dir(args, *parts):
    path = outside_repo(args.out).joinpath(*parts)
    path.mkdir(parents=True, exist_ok=True)
    return path


def header(args, fixture):
    return {"at": utc_stamp(), "schemaVersion": SCHEMA_VERSION, "logicVersion": LOGIC_VERSION,
            "aiGit": git_identity(REPO_ROOT), "fixtureSha256": fixture_hash(fixture),
            "fake": bool(getattr(args, "fake", False)), "budgets": {"setting": SETTING, "typo": TYPO},
            "outputTokens": OUTPUT_TOKENS, "llmConcurrency": LLM_CONCURRENCY}


# --- steps -----------------------------------------------------------------------------------------

def cmd_synthetic(args):
    manuscripts, raw = synthetic.write(step_dir(args, "synthetic"))
    print(json.dumps({"manuscripts": str(manuscripts), "raw": str(raw), "targetNo": synthetic.TARGET_NO}, ensure_ascii=False))


def cmd_build_fixture(args):
    raw = read_json(args.raw)
    fixture, warnings = build(raw, args.manuscripts, target_no=args.target_no, target_file=args.target_file,
                              exclude_target_or_later=args.exclude_target_or_later,
                              source_git={"ai": git_identity(REPO_ROOT)})
    if args.use_local_hashes:
        for row in fixture["episodes"]:
            row["contentHash"] = file_sha256(manuscript_path(args.manuscripts, row["episodeNo"]))
        fixture["approximations"].append(
            f"episode hashes replaced by local manuscript hashes (mismatched: {fixture['localManuscriptHashMismatch']})")
    path = write_json(step_dir(args, "fixture") / "fixture.json", fixture)
    for name, count in sorted(warnings.items()):
        if count:
            print(f"WARNING {name}={count}" + ("" if args.exclude_target_or_later else " (kept; mirrors production)"),
                  file=sys.stderr)
    print(json.dumps({"fixture": str(path), "counts": fixture["counts"], "episodes": len(fixture["episodes"]),
                      "localManuscriptHashMismatch": fixture["localManuscriptHashMismatch"]}, ensure_ascii=False))


def cmd_seed_db(args):
    from scripts.validation_experiment.seed_local_db import apply_migrations, create_database, seed

    local_db_url(args.db_url)
    fixture = read_json(args.fixture)
    engine = create_database(args.db_url)
    try:
        applied = apply_migrations(engine, args.backend_repo, args.backend_ref)
        seeded = seed(engine, fixture)
    finally:
        engine.dispose()
    print(json.dumps({"migrations": len(applied), "last": applied[-1], **seeded}))


async def cmd_index_rag(args):
    from sqlalchemy import create_engine

    from scripts.validation_experiment.index_rag import index

    require_paid(args, "index-rag")
    fixture = read_json(args.fixture)
    directory = step_dir(args, "rag")
    recorder = CallRecorder(directory / "calls.jsonl")
    http = http_client(recorder, args.fake)
    engine = create_engine(local_db_url(args.db_url))
    try:
        client = OpenAIEmbeddingsClient("fake-offline-key" if args.fake else api_key(), "text-embedding-3-small",
                                        1536, "v1", EMBEDDINGS_URL, http)
        result = await index(engine, fixture, args.manuscripts, client)
    finally:
        engine.dispose()
        await http.aclose()
    tokens = result["run"].get("measuredInputTokens") or 0
    write_json(directory / "index.json", {**result, "fake": bool(args.fake),
                                          "estimatedUsd": usd("text-embedding-3-small", tokens, 0)})
    print(json.dumps({"state": result["run"]["state"], "embeddedChunks": result["run"]["embeddedChunks"],
                      "inputTokens": tokens, "ready": result["readiness"]["supportedScopeReady"]}))


async def cmd_plan(args):
    require_paid(args, "plan")
    fixture = read_json(args.fixture)
    directory = step_dir(args, "plan")
    roles = {"PLANNER": runtime(args.model, args.effort, "PLANNER", args.output_tokens)}
    rt = Runtime(args, directory, roles)
    data = FixtureDataClient(fixture, args.manuscripts)
    started = time.monotonic()
    try:
        budget = BudgetState(InvocationBudget(**SETTING))
        frozen = await data.read_input()
        target = (await data.read_source(frozen.target)).text
        subjects = await data.read_subjects()
        planner = SettingPlanner(rt.roles, budget, roles["PLANNER"], stats=Counter())
        segments = planner.segments(target)
        failures = {}
        questions, coverage = await planner.plan(target, segments, subjects, source_catalogue(frozen.sources),
                                                 failures=failures)
    finally:
        await rt.aclose()
    write_json(directory / "planner_outputs.json", {"segments": [segment.__dict__ for segment in segments],
                                                    "batches": {ref: batch.model_dump()
                                                                for ref, batch in planner.batches.items()}})
    write_json(directory / "questions.json", [{"ref": q.ref, "originalRefs": list(q.original_refs),
                                               "draft": q.draft.model_dump()} for q in questions])
    write_json(directory / "result.json", {**header(args, fixture), "model": args.model, "effort": args.effort,
                                           "outputTokens": roles["PLANNER"].max_output_tokens,
                                           "wallSeconds": round(time.monotonic() - started, 3),
                                           "segments": len(segments), "questions": len(questions),
                                           "coverage": coverage, "failures": failures,
                                           "normalizations": dict(planner.stats)})
    print(json.dumps({"segments": len(segments), "questions": len(questions), "failures": failures,
                      "spentUsd": round(rt.ledger.spent_usd(), 6)}))


class ReplayPlanner:
    """Answers PLANNER calls with the saved, already validated batches: judging runs never re-plan."""

    def __init__(self, batches, limit=None):
        self.batches, self.left = batches, limit

    async def count_input_tokens(self, **kwargs):
        return 0

    async def create_text_response(self, **kwargs):
        ref = json.loads(kwargs["user_prompt"])["segment"]["ref"]
        batch = QuestionBatch.model_validate(self.batches[ref])
        if self.left is not None:
            kept = batch.questions[:self.left]
            self.left -= len(kept)
            status = "PLANNED" if kept else "NO_CHECK"
            batch = batch.model_copy(update={"questions": kept, "segments": [
                batch.segments[0].model_copy(update={"status": status, "reason": batch.segments[0].reason})]})
        return LlmTextResponse(text=batch.model_dump_json(), input_token_count=0, output_token_count=0)


class Roles:
    def __init__(self, metered, replay):
        self.metered, self.replay = metered, replay

    def for_validation_role(self, role):
        return self.replay if role == "PLANNER" else self.metered.for_validation_role(role)


async def cmd_judge(args):
    require_paid(args, "judge")
    fixture = read_json(args.fixture)
    plan = read_json(Path(args.plan_dir) / "planner_outputs.json")
    directory = step_dir(args, "judge", args.label)
    judge_runtime = runtime(args.model, args.effort, "JUDGE", args.output_tokens)
    roles = {"PLANNER": judge_runtime, "JUDGE": judge_runtime}
    rt = Runtime(args, directory, {"JUDGE": judge_runtime})
    data = FixtureDataClient(fixture, args.manuscripts)
    search = embedding = engine = None
    if args.db_url:
        from sqlalchemy import create_engine

        from scripts.validation_experiment.local_rag import LocalQueryEmbedding, LocalRagSearch

        engine = create_engine(local_db_url(args.db_url))
        search = LocalRagSearch(engine, fixture)
        embedding = LocalQueryEmbedding(rt.embeddings, rt.ledger, rt.claim.job_id, rt.claim.lease_token,
                                        request_semaphore=rt.semaphore)
    service = SettingValidationService(data, Roles(rt.roles, ReplayPlanner(plan["batches"], args.limit_questions)),
                                       InvocationBudget(**SETTING), roles, search=search, embedding_client=embedding,
                                       experiment=True, judge_concurrency=args.concurrency)
    started = time.monotonic()
    try:
        result = await service.inspect()
    finally:
        await rt.aclose()
        if engine is not None:
            engine.dispose()
    record = {**header(args, fixture), "label": args.label, "model": args.model, "effort": args.effort,
              "outputTokens": judge_runtime.max_output_tokens,
              "rag": "A" if args.db_url else None, "wallSeconds": round(time.monotonic() - started, 3),
              "status": result.status, "failureCode": result.failure_code, "failureCause": result.failure_cause,
              "failureStage": result.failure_stage, "normalizations": result.normalizations,
              "questionFailures": result.question_failures, "traces": result.traces,
              "questions": [{"ref": q.ref, "draft": q.draft.model_dump()} for q in result.questions],
              "decisions": {ref: decision.model_dump() for ref, decision in result.decisions.items()},
              "receipts": [{"ref": r.ref, "operation": r.intent.operation, "kind": r.intent.kind,
                            "query": r.intent.query, "status": r.status, "evidence": list(r.evidence_refs),
                            "reusedFrom": r.reused_from, "failureCode": r.failure_code} for r in result.receipts],
              "embeddingCalls": getattr(embedding, "calls", 0)}
    write_json(directory / "result.json", record)
    print(json.dumps({"status": result.status, "questions": len(result.questions), "decided": len(result.decisions),
                      "failures": result.question_failures, "spentUsd": round(rt.ledger.spent_usd(), 6)},
                     ensure_ascii=False))


async def cmd_typo(args):
    require_paid(args, "typo")
    fixture = read_json(args.fixture)
    directory = step_dir(args, "typo", args.label)
    role = runtime(args.model, args.effort, "TYPO", args.output_tokens)
    rt = Runtime(args, directory, {"TYPO": role})
    started = time.monotonic()
    try:
        result = await check_typos(FixtureDataClient(fixture, args.manuscripts), rt.roles.for_validation_role("TYPO"),
                                   budget=InvocationBudget(**TYPO), role=role)
    finally:
        await rt.aclose()
    write_json(directory / "result.json", {**header(args, fixture), "label": args.label, "model": args.model,
                                           "effort": args.effort, "outputTokens": role.max_output_tokens,
                                           "wallSeconds": round(time.monotonic() - started, 3),
                                           "result": result.model_dump()})
    print(json.dumps({"status": result.status, "failureCode": result.failure_code,
                      "suggestions": len(result.suggestions), "spentUsd": round(rt.ledger.spent_usd(), 6)}))


def cmd_report(args):
    from scripts.validation_experiment.report import write_report

    paths = write_report(outside_repo(args.out), judges=args.judge, typos=args.typo)
    print(json.dumps({key: str(value) for key, value in paths.items()}, ensure_ascii=False))


def parser():
    root = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = root.add_subparsers(dest="command", required=True)

    def add(name, *, paid=False):
        command = sub.add_parser(name)
        command.add_argument("--out", required=True, help="experiment root directory outside the repository")
        if paid:
            command.add_argument("--confirm-paid", action="store_true", help="allow paid provider calls")
            command.add_argument("--fake", action="store_true", help="offline fake provider (no network, no cost)")
            command.add_argument("--spend-cap-usd", type=float, help="stop new calls once all ledgers under --out reach it")
            command.add_argument("--output-tokens", type=int,
                                 help="override the role's production output cap (PLANNER 50000, JUDGE 20000, TYPO 12000)")
        return command

    add("synthetic")
    command = add("build-fixture")
    command.add_argument("--raw", required=True)
    command.add_argument("--manuscripts", required=True)
    command.add_argument("--target-no", type=int, required=True)
    command.add_argument("--target-file")
    command.add_argument("--exclude-target-or-later", action="store_true",
                         help="drop character slots/characters/world settings first sourced from the target or later")
    command.add_argument("--use-local-hashes", action="store_true",
                         help="use local manuscript hashes for prior episodes (when they differ from the export)")
    command = add("seed-db")
    command.add_argument("--fixture", required=True)
    command.add_argument("--db-url", required=True)
    command.add_argument("--backend-repo", required=True)
    command.add_argument("--backend-ref", default="origin/main")
    command = add("index-rag", paid=True)
    command.add_argument("--fixture", required=True)
    command.add_argument("--manuscripts", required=True)
    command.add_argument("--db-url", required=True)
    command = add("plan", paid=True)
    for name in ("--fixture", "--manuscripts"):
        command.add_argument(name, required=True)
    command.add_argument("--model", default="gpt-6.1-terra")
    command.add_argument("--effort", default="medium")
    command = add("judge", paid=True)
    for name in ("--fixture", "--manuscripts", "--plan-dir", "--label"):
        command.add_argument(name, required=True)
    command.add_argument("--model", required=True)
    command.add_argument("--effort", default="medium")
    command.add_argument("--db-url", help="local experiment DB with the RAG index (omit: no search offered)")
    command.add_argument("--concurrency", type=int, default=4)
    command.add_argument("--limit-questions", type=int, help="judge only the first N planned questions (smoke)")
    command = add("typo", paid=True)
    for name in ("--fixture", "--manuscripts", "--label"):
        command.add_argument(name, required=True)
    command.add_argument("--model", required=True)
    command.add_argument("--effort", default="low")
    command = add("report")
    command.add_argument("--judge", action="append", default=[], help="judge label (repeat; first two are compared)")
    command.add_argument("--typo", action="append", default=[], help="typo label (repeat; first two are compared)")
    return root


def main(argv=None):
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s %(message)s")
    args = parser().parse_args(argv)
    handlers = {"synthetic": cmd_synthetic, "build-fixture": cmd_build_fixture, "seed-db": cmd_seed_db,
                "index-rag": cmd_index_rag, "plan": cmd_plan, "judge": cmd_judge, "typo": cmd_typo,
                "report": cmd_report}
    try:
        outcome = handlers[args.command](args)
        if asyncio.iscoroutine(outcome):
            asyncio.run(outcome)
    except (HarnessError, IncompleteInspection) as exc:
        print(f"ERROR {type(exc).__name__}: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
