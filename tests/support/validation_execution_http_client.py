"""Real Java HTTP + real Worker driver; only the paid provider is synthetic.

Never import this module into product code. Numeric limits come from Java test fixtures.
"""
import argparse
import asyncio
import json
import time
from collections import Counter
from pathlib import Path

import httpx

from app.llm.openai_client import OpenAIResponsesClient
from app.validation.client import ValidationExecutionClient
from app.validation.contracts import IncompleteInspection
from app.worker.episode_validation_worker import EpisodeValidationWorker


class CrashClient(ValidationExecutionClient):
    def __init__(self, *args, mode, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode

    async def leased(self, claim, operation, **kwargs):
        if self.mode in {"book-failure-crash", "vector-missing-crash"} and operation == "complete" and kwargs.get("failureCode"):
            raise asyncio.CancelledError()
        result = await super().leased(claim, operation, **kwargs)
        if ((self.mode == "vector-lookup-crash" and operation == "checkpoint"
             and kwargs.get("payload", {}).get("phase") == "JUDGING"
             and not kwargs["payload"]["runtime"]["pending"]["Q1"])
            or (self.mode == "crash-plan" and operation == "plans" and kwargs.get("stage") == "MERGED_PLAN")
            or (self.mode == "crash-decision" and operation == "units" and kwargs.get("branch") == "SETTING")
            or (self.mode == "followup-crash" and operation == "revisions")):
            raise asyncio.CancelledError()
        return result


class SyntheticProvider:
    def __init__(self, mode, wait_file=None):
        self.mode, self.wait_file = mode, wait_file
        self.calls, self.judges = Counter(), Counter()

    async def __call__(self, request):
        body = json.loads(request.content)
        if request.url.path.endswith("embeddings"):
            assert body["model"] == "text-embedding-3-small" and body["dimensions"] == 1536
            self.calls["QUERY_EMBEDDING"] += 1
            return httpx.Response(200, json={"model": "text-embedding-3-small", "usage": {"prompt_tokens": 10},
                "data": [{"index": 0, "embedding": [1.0] + [0.0] * 1535}]})
        assert body["model"] == "gpt-6.1-terra"
        if request.url.path.endswith("input_tokens"):
            return httpx.Response(200, json={"input_tokens": 23})
        payload = json.loads(body["input"][1]["content"][0]["text"])
        role = payload.get("role", "TYPO")
        self.calls[role] += 1
        assert body["reasoning"]["effort"] == {
            "PLANNER": "medium", "RELATED_SELECTION": "high", "COVERAGE": "high",
            "JUDGE": "medium", "TYPO": "low"}[role]
        if self.mode == "all-fail" or (role == "JUDGE" and self.mode in {"judge-fail", "vector-judge-fail"}) or (
            role == "TYPO" and self.mode == "typo-fail"):
            return httpx.Response(503)
        if role == "TYPO":
            result = {"segments": [{"segment_ref": payload["segment_ref"], "status": "NO_CHECK", "reason": "합성 검사"}],
                      "suggestions": []}
        elif role == "RELATED_SELECTION":
            result = {"entries": [{"entry_ref": row["entry_ref"], "selected": True, "reason": "합성 선정"}
                                  for row in payload["catalogue"]]}
        elif role in {"PLANNER", "COVERAGE"}:
            planned = role == "COVERAGE" and not payload.get("raw_rules") and self.mode not in {
                "zero", "all-fail", "typo-fail", "book-failure-crash"}
            count = 3 if self.mode.startswith("followup") else 1
            questions = [self.question(payload, index) for index in range(1, count + 1)] if planned else []
            if self.mode.startswith("vector"):
                for question in questions:
                    question["lookup_intents"] = [{"ref": "vector-1", "kind": "CHANGES_EXCEPTIONS",
                        "operation": "VECTOR", "purpose": "과거 고정 의미 검색", "query": "  합성 의수  "}]
            result = {"questions": questions,
                "segments": [{"segment_ref": payload["segment"]["ref"], "status": "PLANNED" if questions else "NO_CHECK", "reason": "합성 처리"}],
                "rules": [{"entry_ref": row["ref"] if "raw_rules" in payload else row["entry_ref"],
                           "status": "CHECKED", "reason": "합성 규칙 확인"}
                          for row in payload.get("raw_rules", payload.get("settings", []))],
                "clue_checks": [{"entry_ref": row["ref"], "status": "CHECKED", "reason": "서버 단서 확인"}
                                for row in payload.get("mechanical_clues", [])]}
        else:
            self.judges[payload["question_ref"]] += 1
            if self.wait_file:
                print("GH310_PROVIDER_READY", flush=True)
                until = time.monotonic() + 20
                while not Path(self.wait_file).exists():
                    assert time.monotonic() < until, "synthetic provider synchronization expired"
                    await asyncio.sleep(0.01)
            result = self.decision(payload)
            if self.mode == "followup-crash" and payload["question_ref"] == "Q3" and payload["check_revision"] == 1:
                result.update(action="FOLLOW_UP", outcome=None, basis=None, completion=None,
                    impact=None, impact_reason=None, affected_question_refs=["Q1"],
                    follow_ups=[{"ref": "new-context", "kind": "NEW_CLUE", "operation": "TARGET_CONTEXT",
                                 "purpose": "같은 문맥 추가 확인", "source_ref": "T1", "start": 0, "end": 2}])
        await asyncio.sleep(0)
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(result, ensure_ascii=False),
                                        "usage": {"input_tokens": 23, "output_tokens": 7}})

    @staticmethod
    def question(payload, index):
        start = payload["segment"]["start"]
        quote = payload["text"][start - payload["text_start"]:start - payload["text_start"] + 2]
        subjects = [payload["settings"][0]["subject_ref"]] if payload["settings"] else []
        return {"local_ref": f"local-{index}", "target_spans": [{"source_ref": "T1", "start": start, "end": start + len(quote), "quote": quote}],
            "subject_refs": subjects, "subject_mentions": ["합성 인물"], "property_or_rule": f"합성 비교 {index}",
            "time_context": "현재", "conditions": [], "assertion_mode": "NARRATION", "check_intent": "고정 원문 대조",
            "reason": "실제 과거 출처 확인", "lookup_intents": [{"ref": f"read-{index}", "kind": "COMPARISON_CONTEXT",
                "operation": "DIRECT", "purpose": "고정 과거 원고 직접 확인", "source_ref": "R1", "start": 0, "end": 2}]}

    def decision(self, payload):
        refs = ["T1", *[row["entry_ref"] for row in payload["settings"]], *[row["ref"] for row in payload["evidence"]]]
        assessment = {"reason": "같은 대상·문맥과 실제 조회를 확인함", "evidence_refs": refs,
                      "receipt_refs": [row["ref"] for row in payload["receipts"]]}
        conflict = self.mode == "conflict"
        return {"action": "DECIDED", "outcome": "CONFLICT" if conflict else "NO_CONFLICT",
            "basis": "DIRECT_CONTRADICTION" if conflict else "COMPATIBLE", "reason": "합성 검증 결과",
            "impact": "CAUTION" if conflict else None, "impact_reason": "합성 비교 차이" if conflict else None,
            "target_spans": payload["question"]["target_spans"], "evidence_refs": refs,
            "completion": {key: assessment for key in ("comparison_context", "changes_exceptions", "no_pending_clues", "required_reads")},
            "follow_ups": [], "affected_question_refs": [], "disagreements": []}


async def main(args):
    provider = SyntheticProvider(args.mode, args.wait_file)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        raw = OpenAIResponsesClient("synthetic-only", "gpt-6.1-terra", "https://provider.invalid/responses", http_client=http)
        client = CrashClient(args.base_url, "gh310-synthetic-internal", mode=args.mode)
        embedding, sessions, engine = None, None, None
        if args.readonly_url:
            from sqlalchemy import create_engine
            from sqlalchemy.engine import make_url
            from sqlalchemy.orm import sessionmaker

            from app.embeddings.client import OpenAIEmbeddingsClient
            parsed = make_url(args.readonly_url)
            assert parsed.host == "127.0.0.1" and parsed.database == "gh310_execution_test"
            engine = create_engine(args.readonly_url, connect_args={"options": "-c default_transaction_read_only=on"})
            sessions = sessionmaker(engine, autoflush=False, expire_on_commit=False)
            embedding = OpenAIEmbeddingsClient("synthetic", "text-embedding-3-small", 1536, "v1",
                                              "https://provider.invalid/embeddings", http)
        worker = EpisodeValidationWorker(client, raw, enabled=True, worker_id="gh310-http-synthetic",
                                         readonly_sessions=sessions, embedding_client=embedding)
        claim = await worker.claim_next()
        assert claim is not None, "fixture must have queued validation"
        state = None
        try:
            state = await worker.process_claimed(claim)
        except asyncio.CancelledError:
            state = {"crashed": True}
        except IncompleteInspection as exc:
            state = {"stopped": exc.code}
        finally:
            await worker.aclose()
            if engine is not None:
                engine.dispose()
        print("GH310_WORKER_RESULT " + json.dumps({"calls": provider.calls, "judges": provider.judges, "result": state}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--wait-file")
    parser.add_argument("--readonly-url")
    asyncio.run(main(parser.parse_args()))
