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
from app.validation.contracts import IncompleteInspection, QuestionDraft
from app.validation.judging import target_search_query
from app.validation.projection import setting_value_leaves
from app.worker.episode_validation_worker import EpisodeValidationWorker


class CrashClient(ValidationExecutionClient):
    def __init__(self, *args, mode, **kwargs):
        super().__init__(*args, **kwargs)
        self.mode = mode

    async def leased(self, claim, operation, **kwargs):
        if self.mode == "book-failure-crash" and operation == "complete" and kwargs.get("failureCode"):
            raise asyncio.CancelledError()
        result = await super().leased(claim, operation, **kwargs)
        bundle = kwargs.get("payload", {}).get("evidence_bundle") or {}
        receipts = bundle.get("receipts", [])
        setting_unit = operation == "units" and kwargs.get("branch") == "SETTING"
        if ((self.mode in {"vector-lookup-crash", "vector-fill-crash"} and setting_unit
             and any(row["intent"]["operation"] == "VECTOR" for row in receipts))
            # Grid v4: VECTOR is only searched while the index is ready, so the "missing embedding" retry case
            # stores the merged plan and crashes before any judge runs.
            or (self.mode == "vector-missing-crash" and operation == "plans" and kwargs.get("stage") == "MERGED_PLAN")
            or (self.mode == "crash-plan" and operation == "plans" and kwargs.get("stage") == "MERGED_PLAN")
            or (self.mode == "crash-decision" and setting_unit)
            # No /revisions in grid v4: crash after publishing the decision that used a follow-up context read.
            or (self.mode == "followup-crash" and setting_unit and bundle.get("additional_target_context"))):
            raise asyncio.CancelledError()
        return result


# The Java synthetic fixture freezes the production pairing (configuration v2): PLANNER Terra medium, JUDGE Luna high,
# TYPO Terra low. Every call must carry its role's frozen model and effort.
RUNTIMES = {"PLANNER": ("gpt-6.1-terra", "medium"), "JUDGE": ("gpt-6-luna", "high"), "TYPO": ("gpt-6.1-terra", "low")}


class SyntheticProvider:
    """`subject_mentions` is accepted for older Java drivers and ignored: grid v4 has no SUBJECT_RESOLUTION."""

    def __init__(self, mode, wait_file=None, subject_mentions="none"):
        self.mode, self.wait_file, self.subject_mentions = mode, wait_file, subject_mentions
        self.calls, self.judges = Counter(), Counter()
        self.queries = set()  # stripped search sentences this synthetic judge asked for

    async def __call__(self, request):
        body = json.loads(request.content)
        if request.url.path.endswith("embeddings"):
            assert body["model"] == "text-embedding-3-small" and body["dimensions"] == 1536
            # The search sentence of the vector modes; anything else is an intake index fill of retrieval chunks.
            query = any("의수" in value or value.strip() in self.queries for value in body["input"])
            role = "QUERY_EMBEDDING" if query else "INDEX_EMBEDDING"
            self.calls[role] += 1
            if role == "INDEX_EMBEDDING" and self.mode == "vector-fill-fail":
                return httpx.Response(503)
            return httpx.Response(200, json={"model": "text-embedding-3-small", "usage": {"prompt_tokens": 10},
                "data": [{"index": index, "embedding": [1.0] + [0.0] * 1535}
                         for index in range(len(body["input"]))]})
        if request.url.path.endswith("input_tokens"):
            assert body["model"] in {model for model, _ in RUNTIMES.values()}
            return httpx.Response(200, json={"input_tokens": 23})
        payload = json.loads(body["input"][1]["content"][0]["text"])
        role = payload.get("role", "TYPO")
        self.calls[role] += 1
        assert (body["model"], body["reasoning"]["effort"]) == RUNTIMES[role]
        if self.mode == "all-fail" or (role == "JUDGE" and self.mode in {"judge-fail", "vector-judge-fail"}) or (
            role == "TYPO" and self.mode == "typo-fail"):
            return httpx.Response(503)
        if role == "TYPO":
            result = {"segments": [{"segment_ref": payload["segment_ref"], "status": "NO_CHECK", "reason": "합성 검사"}],
                      "suggestions": []}
        elif role == "PLANNER":
            planned = self.mode not in {"zero", "all-fail", "typo-fail", "book-failure-crash"}
            count = 3 if self.mode.startswith("followup") else 1
            questions = [self.question(payload, index) for index in range(1, count + 1)] if planned else []
            result = {"questions": questions,
                      "segments": [{"segment_ref": payload["segment"]["ref"],
                                    "status": "PLANNED" if questions else "NO_CHECK", "reason": "합성 처리"}]}
        else:
            self.judges[payload["question_ref"]] += 1
            if self.wait_file:
                print("GH310_PROVIDER_READY", flush=True)
                until = time.monotonic() + 20
                while not Path(self.wait_file).exists():
                    assert time.monotonic() < until, "synthetic provider synchronization expired"
                    await asyncio.sleep(0.01)
            result = self.decision(payload)
            if payload["form"] == "OPEN" and self.mode.startswith("vector"):
                # One natural-language VECTOR search through the frozen index; the query identity is hashed exactly.
                # Since 2026-11-03 the Worker adds its own target-text search to every searching FOLLOW_UP; this
                # judge asks for that same sentence (padded), so the run-level reuse keeps one query embedding and the
                # Java fixtures' QUERY_EMBEDDING cap of 1 still holds.
                query = self.worker_target_query(payload) or "합성 의수"
                self.queries.add(query)
                result = self.follow_up(payload, search_queries=[f"  {query}  "])
            elif payload["form"] == "OPEN" and self.mode == "followup-crash" and payload["question_ref"] == "Q3":
                result = self.follow_up(payload, context_requests=[{"start": 0, "end": 2}])
        await asyncio.sleep(0)
        return httpx.Response(200, json={"status": "completed", "output_text": json.dumps(result, ensure_ascii=False),
                                        "usage": {"input_tokens": 23, "output_tokens": 7}})

    @staticmethod
    def question(payload, index):
        start = payload["segment"]["start"]
        quote = payload["text"][start - payload["text_start"]:start - payload["text_start"] + 2]
        subjects = [payload["subject_catalogue"][0]["subject_ref"]] if payload["subject_catalogue"] else []
        return {"local_ref": f"local-{index}", "target_spans": [{"source_ref": "T1", "start": start, "end": start + len(quote), "quote": quote}],
            "subject_refs": subjects, "subject_mentions": ["합성 인물"], "property_or_rule": f"합성 비교 {index}",
            "time_context": "현재", "conditions": [], "assertion_mode": "NARRATION", "check_intent": "고정 원문 대조",
            "reason": "실제 과거 출처 확인"}

    @staticmethod
    def worker_target_query(payload):
        """The Worker's target-text query rebuilt from the payload (target_context covers ±300 around each span)."""
        contexts = payload.get("target_context") or []
        if not contexts:
            return ""
        chars = ["\x00"] * max(row["start"] + len(row["text"]) for row in contexts)
        for row in contexts:
            chars[row["start"]:row["start"] + len(row["text"])] = row["text"]
        return target_search_query(QuestionDraft.model_validate(payload["question"]), "".join(chars))

    @staticmethod
    def follow_up(payload, search_queries=(), context_requests=()):
        return {"action": "FOLLOW_UP", "outcome": None, "basis": None, "reason": "DB 값만으로 판단할 수 없음",
                "impact": None, "impact_reason": None, "target_spans": payload["question"]["target_spans"],
                "evidence_refs": [], "completion": None, "disagreements": [],
                "search_queries": list(search_queries), "context_requests": list(context_requests)}

    @staticmethod
    def basis_quotes(payload):
        """One B quote (a stored value substring) and one E quote (a chunk prefix) so the server verifies both."""
        quotes = []
        for row in payload["settings"][:1]:
            leaf = next((value for value in setting_value_leaves(row) if value.strip()), None)
            if leaf is not None:
                quotes.append({"ref": row["entry_ref"], "quote": leaf.strip()[:6]})
        for row in payload["evidence"][:1]:
            if row["quote"][:8].strip():
                quotes.append({"ref": row["ref"], "quote": row["quote"][:8]})
        return quotes

    def decision(self, payload):
        refs = ["T1", *[row["entry_ref"] for row in payload["settings"]], *[row["ref"] for row in payload["evidence"]]]
        assessment = {"reason": "같은 대상·문맥과 실제 조회를 확인함", "evidence_refs": refs,
                      "receipt_refs": [row["ref"] for row in payload["receipts"]]}
        conflict = self.mode == "conflict"
        result = {"action": "DECIDED", "outcome": "CONFLICT" if conflict else "NO_CONFLICT",
            "basis": "DIRECT_CONTRADICTION" if conflict else "COMPATIBLE", "reason": "합성 검증 결과",
            "impact": "CAUTION" if conflict else None, "impact_reason": "합성 비교 차이" if conflict else None,
            "target_spans": payload["question"]["target_spans"], "evidence_refs": refs,
            "completion": {key: assessment for key in ("comparison_context", "changes_exceptions", "no_pending_clues", "required_reads")},
            "disagreements": [], "basis_quotes": self.basis_quotes(payload)}
        if payload["form"] == "OPEN":
            result.update(search_queries=[], context_requests=[])
        return result


async def main(args):
    provider = SyntheticProvider(args.mode, args.wait_file, args.subject_mentions)
    async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as http:
        raw = OpenAIResponsesClient("synthetic-only", "gpt-6.1-terra", "https://provider.invalid/responses", http_client=http)
        client = CrashClient(args.base_url, "gh310-synthetic-internal", mode=args.mode)
        embedding, sessions, engine, index_sessions, writer = None, None, None, None, None
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
        if args.index_url:
            from sqlalchemy import create_engine
            from sqlalchemy.engine import make_url
            from sqlalchemy.orm import sessionmaker

            parsed = make_url(args.index_url)
            assert parsed.host == "127.0.0.1" and parsed.database == "gh310_execution_test"
            writer = create_engine(args.index_url)
            index_sessions = sessionmaker(writer, autoflush=False, expire_on_commit=False)
        worker = EpisodeValidationWorker(client, raw, enabled=True, worker_id="gh310-http-synthetic",
                                         readonly_sessions=sessions, embedding_client=embedding,
                                         index_sessions=index_sessions)
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
            if writer is not None:
                writer.dispose()
        print("GH310_WORKER_RESULT " + json.dumps({"calls": provider.calls, "judges": provider.judges, "result": state}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--wait-file")
    parser.add_argument("--readonly-url")
    parser.add_argument("--index-url", help="Writer URL of the intake index fill (retrieval tables only)")
    parser.add_argument("--subject-mentions", choices=("none", "resolved", "null"), default="none")
    asyncio.run(main(parser.parse_args()))
