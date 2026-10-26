"""Two real Python QA executions with scripted provider/Spring inputs; no paid calls.

python -m tests.support.chat_evaluation_replay --output-dir /tmp/chat-eval-smoke-unique
This bridge tests capture -> score, not a 26-question or three-strategy quality baseline.
"""

import argparse
import asyncio
import copy
from dataclasses import asdict
from hashlib import sha256
import json
from pathlib import Path
import subprocess
from unittest.mock import patch

from app.chatbot.evaluation.__main__ import json_text, write
from app.chatbot.evaluation.contracts import Dataset, Observation, Run, fingerprint
from app.chatbot.evaluation.scoring import export_inputs
from app.chatbot.replay import replay_case
from app.chatbot.qa_prompts import PLAN_VERSION, SELECT_VERSION
from app.schemas.chat_execution import ConversationTurn

ROOT = Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "tests/fixtures/chat-evaluation-v1"


def reject_network(*args, **kwargs):
    raise RuntimeError("Evaluation replay cannot use external services")


async def capture(inputs, runtime_fixture):
    """This function receives a gold-free projection and independent scripted model inputs."""
    traces, records = [], []
    corpus = {e["id"]: e for e in inputs["evidence"]}
    for case in inputs["questions"]:
        if case["id"] not in {"current_manual_override", "followup_refresh"}:
            continue
        tool_responses = []
        usage_by_id = {}

        def setup(spring):
            spring.context = spring.context.model_copy(
                update={
                    "conversation": [ConversationTurn(**t) for t in case["conversation"]],
                    "conversation_truncated": case["conversation_truncated"],
                }
            )
            lookup = spring.lookup

            def record_lookup(query):
                result = lookup(query)
                tool_responses.append(copy.deepcopy(result))
                return result

            spring.lookup = record_lookup
            respond = spring.respond

            def record_response(request):
                if "/calls/" in request.url.path:
                    usage_by_id[request.url.path.rsplit("/", 1)[-1]] = json.loads(request.content)
                return respond(request)

            spring.respond = record_response

        trace = await replay_case(
            {
                "id": case["id"],
                "question": case["question"],
                "modelOutputs": runtime_fixture["outputs"],
            },
            runtime_fixture["catalog"],
            spring_setup=setup,
        )
        trace["toolResponses"] = tool_responses
        for request in trace["modelRequests"]:
            request["response_schema"] = asdict(request["response_schema"])
        traces.append(trace)
        final = trace["finish"]
        calls = []
        for reserved in trace["reservations"]:
            usage = usage_by_id.get(reserved["callId"], {})
            calls.append(
                {
                    "call_id": reserved["callId"],
                    "purpose": "PLAN" if reserved["purpose"] == "CHAT_INTERPRETATION" else "SELECT",
                    "outcome": usage.get("outcome") or "UNKNOWN",
                    "input_tokens": usage.get("inputTokens"),
                    "cached_input_tokens": usage.get("cachedInputTokens"),
                    "output_tokens": usage.get("outputTokens"),
                }
            )
        telemetry = len(usage_by_id) == len(trace["reservations"])
        if final["failure"] is not None:
            records.append(
                {
                    "case_id": case["id"],
                    "status": "FAILED",
                    "failure_code": final["failure"],
                    "calls": calls,
                    "telemetry_complete": telemetry,
                }
            )
            continue
        rows = [
            item
            for response in tool_responses
            if response["tool"] == "WORLD_CURRENT"
            for item in response["items"]["content"]
        ]
        if len(rows) != 1 or rows[0]["origin"] != "USER_INPUT":
            raise ValueError("Smoke capture no longer matches its documented input contract")
        row = rows[0]
        normalized = (
            f"사용자가 직접 수정한 {row['subjectName']}의 현재 "
            f"{row['displayName']}은 {row['value']}다."
        )
        evidence = corpus["fire-current"]
        if normalized != evidence["text"]:
            raise ValueError("Observed RDB row differs from the declared corpus snapshot")
        records.append(
            {
                "case_id": case["id"],
                "status": "COMPLETED",
                "outcome": final["result"]["outcome"],
                "text": final["result"]["text"],
                "observed_evidence_ids": [evidence["id"]],
                # An internal row reference for semantic review; not a fabricated original quote.
                "citations": [
                    {
                        "evidence_id": evidence["id"],
                        "version": evidence["version"],
                        "sha256": evidence["sha256"],
                        "quote": normalized,
                        "start": 0,
                        "end": len(normalized),
                    }
                ],
                "calls": calls,
                "telemetry_complete": telemetry,
                "latency_ms": None,
            }
        )
    return traces, records


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    dataset = Dataset.model_validate_json((FIXTURE / "dataset.json").read_text())
    runtime_fixture = json.loads((FIXTURE / "runtime-smoke.json").read_text())
    with (
        patch("socket.socket.connect", reject_network),
        patch("socket.socket.connect_ex", reject_network),
    ):
        traces, observations = asyncio.run(capture(export_inputs(dataset), runtime_fixture))
    code_sha = subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=ROOT,
        text=True,
    ).strip()
    trace = {
        "kind": "SCRIPTED_REPLAY",
        "observations_sha256": fingerprint(
            [Observation.model_validate(item).model_dump(mode="json") for item in observations]
        ),
        "provider_calls": "synthetic usage; not cost measurement",
        "runtime_fixture_sha256": sha256((FIXTURE / "runtime-smoke.json").read_bytes()).hexdigest(),
        "runtime_files": {
            name: sha256((ROOT / name).read_bytes()).hexdigest()
            for name in (
                "app/chatbot/qa.py",
                "app/chatbot/qa_render.py",
                "app/chatbot/replay.py",
                "app/chatbot/runtime.py",
                "app/chatbot/qa_prompts.py",
                "tests/support/chat_evaluation_replay.py",
            )
        },
        "executions": traces,
    }
    trace_text = json_text(trace)
    run = Run.model_validate(
        {
            "manifest": {
                "schema_version": 1,
                "run_id": "qa-scripted-smoke-v1",
                "dataset_sha256": fingerprint(dataset),
                "mode": "RDB_DIRECT",
                "execution_kind": "SCRIPTED_REPLAY",
                "trace_sha256": sha256(trace_text.encode()).hexdigest(),
                "conditions": {
                    "code_sha": code_sha,
                    "model": "ScriptedTextClient",
                    "reasoning": "unmeasured",
                    "prompt_versions": {"plan": PLAN_VERSION, "select": SELECT_VERSION},
                    "index_version": "not-used",
                    "embedding_model": "not-used",
                    "embedding_dimensions": 1536,
                    "cache": "UNMEASURED",
                    "concurrency": 1,
                },
                "budgets": {"max_tool_calls": 16, "max_selected_rows": 10},
            },
            "observations": observations,
        }
    )
    args.output_dir.mkdir(parents=True, exist_ok=False, mode=0o700)
    write(args.output_dir / "trace.json", trace_text)
    write(args.output_dir / "run.json", json_text(run.model_dump(mode="json")))
    print(f"Captured {len(observations)} scripted QA executions; semantic review pending.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
