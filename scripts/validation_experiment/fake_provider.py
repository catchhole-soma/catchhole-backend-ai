"""Offline OpenAI stand-in for harness dry runs (httpx.MockTransport). No network, no key, no cost.

The planner asks one question per sentence of the segment core; the judge follows a fixed script that covers
every flow: Q1 FOLLOW_UP with a search (plus the Worker's target-text search) then FINAL, Q2 the same search (cache
hit) plus a context read then a SUSPECTED_CONFLICT quoting an earlier sentence that triggers the Worker change
search and a FINAL call, Q3 an unparsable answer (contract error), Q4 a CONFLICT without any basis quote on the
OPEN call (the judge-rule guard publishes it as NO_CONFLICT), every other question DECIDED NO_CONFLICT on the OPEN
call. Typo answers NO_CHECK. Embeddings are deterministic non-zero unit vectors.
"""

import json
import math
import re
from hashlib import sha256

import httpx

from app.validation.projection import setting_value_leaves

SHARED_QUERY = "주인공이 예전에 사용한 능력과 그 조건"


def _response(result, tokens_in, tokens_out=40):
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return httpx.Response(200, json={"id": "fake-" + sha256(text.encode()).hexdigest()[:12], "status": "completed",
                                     "output_text": text, "usage": {"input_tokens": tokens_in, "output_tokens": tokens_out,
                                                                    "input_tokens_details": {"cached_tokens": 0}}})


def _sentences(text, offset, start, end):
    for match in re.finditer(r"[^\n.!?]+[.!?]?", text):
        absolute = offset + match.start()
        quote = match.group().strip()
        if len(quote) < 4:
            continue
        absolute += match.group().index(quote)
        if start <= absolute and absolute + len(quote) <= end:
            yield absolute, quote


def planner(payload):
    segment = payload["segment"]
    questions = []
    for index, (start, quote) in enumerate(_sentences(payload["text"], payload["text_start"], segment["start"],
                                                       segment["end"]), 1):
        subjects = [row["subject_ref"] for row in payload["subject_catalogue"]
                    if row["name"] and row["name"] in quote][:2]
        questions.append({"local_ref": f"s{index}", "target_spans": [{"source_ref": "T1", "start": start,
                                                                      "end": start + len(quote), "quote": quote}],
                          "subject_refs": subjects, "subject_mentions": [quote.split()[0]],
                          "property_or_rule": f"문장 {index}의 설정", "time_context": "현재", "conditions": [],
                          "assertion_mode": "NARRATION", "check_intent": "기존 설정과 대조", "reason": "합성 질문"})
        if len(questions) == 6:
            break
    return {"questions": questions, "segments": [{"segment_ref": segment["ref"],
                                                  "status": "PLANNED" if questions else "NO_CHECK",
                                                  "reason": "합성 계획"}]}


def _first_quote(payload):
    """An exact earlier sentence for a finding: the first evidence chunk's opening, else a stored setting value."""
    for row in payload["evidence"]:
        quote = row["quote"][:40].strip()
        if quote:
            return [{"ref": row["ref"], "quote": quote}]
    for row in payload["settings"]:
        leaf = next((value.strip()[:40] for value in setting_value_leaves(row) if value.strip()), None)
        if leaf:
            return [{"ref": row["entry_ref"], "quote": leaf}]
    return []


def _decided(payload, outcome="NO_CONFLICT", quotes=()):
    facts = [row["entry_ref"] for row in payload["settings"]]
    refs = ["T1", *facts, *(row["ref"] for row in payload["evidence"])]
    assessment = {"reason": "합성 판정 근거", "evidence_refs": refs,
                  "receipt_refs": [row["ref"] for row in payload["receipts"]]}
    finding = outcome != "NO_CONFLICT"
    result = {"action": "DECIDED", "outcome": outcome,
              "basis": {"NO_CONFLICT": "COMPATIBLE", "SUSPECTED_CONFLICT": "CHANGE_NOT_FOUND",
                        "CONFLICT": "DIRECT_CONTRADICTION"}[outcome],
              "headline": "합성 대상의 확인 항목이 기존 설정과 달라요" if finding else None,
              "reason": "합성 판정", "impact": "CAUTION" if finding else None,
              "impact_reason": "합성 영향" if finding else None, "target_spans": payload["question"]["target_spans"],
              "evidence_refs": (facts or refs) if finding else refs,
              "completion": {key: dict(assessment) for key in ("comparison_context", "changes_exceptions",
                                                               "no_pending_clues", "required_reads")},
              "disagreements": [], "basis_quotes": list(quotes)}
    if payload["form"] == "OPEN":
        result.update(search_queries=[], context_requests=[])
    return result


def _follow_up(payload, *, contexts=()):
    return {"action": "FOLLOW_UP", "outcome": None, "basis": None, "reason": "DB 값만으로 판단 불가", "impact": None,
            "impact_reason": None, "target_spans": payload["question"]["target_spans"], "evidence_refs": [],
            "completion": None, "disagreements": [], "search_queries": [SHARED_QUERY],
            "context_requests": [{"start": start, "end": end} for start, end in contexts]}


def judge(payload):
    qref, form = payload["question_ref"], payload["form"]
    if qref == "Q3":
        return "{not json"
    if qref in {"Q1", "Q2"} and form == "OPEN":
        span = payload["question"]["target_spans"][0]
        return _follow_up(payload, contexts=[(span["start"], span["end"])] if qref == "Q2" else ())
    if qref == "Q2" and payload["question"]["subject_refs"]:
        return _decided(payload, "SUSPECTED_CONFLICT", _first_quote(payload))
    if qref == "Q4" and form == "OPEN":
        return _decided(payload, "CONFLICT")  # no basis quote: the Worker's rule guard clears it
    return _decided(payload)


def typo(payload):
    return {"segments": [{"segment_ref": payload["segment_ref"], "status": "NO_CHECK", "reason": "합성 검사"}],
            "suggestions": []}


def _vector(text, dimensions=1536):
    seed = sha256(text.encode()).digest()
    values = [((seed[index % 32] + index) % 17) - 8 + 0.5 for index in range(dimensions)]
    norm = math.sqrt(sum(value * value for value in values))
    return [value / norm for value in values]


async def handler(request):
    body = json.loads(request.content)
    path = request.url.path
    if path.endswith("embeddings"):
        inputs = body["input"]
        return httpx.Response(200, json={"model": body["model"], "usage": {"prompt_tokens": sum(len(x) for x in inputs)},
                                         "data": [{"index": i, "embedding": _vector(text)} for i, text in enumerate(inputs)]})
    prompt = body["input"][1]["content"][0]["text"]
    if path.endswith("input_tokens"):
        return httpx.Response(200, json={"input_tokens": len(prompt) // 2 + 500})
    payload = json.loads(prompt)
    role = payload.get("role", "TYPO")
    result = {"PLANNER": planner, "JUDGE": judge}.get(role, typo)(payload)
    return _response(result, len(prompt) // 2 + 500)


def transport():
    return httpx.MockTransport(handler)
