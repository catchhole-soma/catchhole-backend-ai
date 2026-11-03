"""Local usage ledger and provider-response recorder for the experiment (no Spring, no headers, no keys).

`LocalLedger` implements the metering ledger protocol, so the real `MeteredTextGenerationClient`
(`exact_input_reservation=True`, `max_retries=0`) reserves and settles every call here: role, model, reserved,
input, cached and output tokens, outcome and latency. `CallRecorder` is an httpx event hook pair that writes one
JSON line per provider response: identifiers parsed from the request body (role, form, question, segment, model,
effort, schema name), usage and the raw output text. Request headers are never read.
"""

import asyncio
import json
import time
from hashlib import sha256
from pathlib import Path


class LocalLedger:
    def __init__(self, path, efforts=None, *, spend_cap_usd=None, price=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.efforts = efforts or {}
        self.rows, self.pending = [], {}
        self.spend_cap_usd, self.price = spend_cap_usd, price
        self._lock = asyncio.Lock()

    def spent_usd(self):
        if self.price is None:
            return 0.0
        return sum(self.price(row["model"], row.get("inputTokens") or 0, row.get("outputTokens") or 0) or 0.0
                   for row in self.rows)

    async def reserve_ai_tokens(self, request_id, analysis_job_id, purpose, attempt, model_name, reserved_tokens,
                                lease_token):
        if self.spend_cap_usd is not None and self.spent_usd() >= self.spend_cap_usd:
            from app.validation.contracts import IncompleteInspection

            raise IncompleteInspection("AI_TOKEN_QUOTA_EXHAUSTED")  # local spend cap reached: no new call
        role = purpose.removeprefix("VALIDATION_")
        self.pending[request_id] = {"requestId": str(request_id), "role": role, "model": model_name,
                                    "effort": self.efforts.get(role), "reservedTokens": reserved_tokens,
                                    "attempt": attempt, "reservedAt": time.time(), "started": time.monotonic()}

    async def _finish(self, request_id, **values):
        async with self._lock:
            row = self.pending.pop(request_id, None)
            if row is None:
                return
            row["latencyMs"] = int((time.monotonic() - row.pop("started")) * 1000)
            row.update(values)
            self.rows.append(row)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + "\n")

    async def settle_ai_tokens(self, request_id, input_tokens, cached_input_tokens, output_tokens, outcome):
        await self._finish(request_id, kind="SETTLE", inputTokens=input_tokens, cachedInputTokens=cached_input_tokens,
                           outputTokens=output_tokens, outcome=outcome)

    async def release_ai_tokens(self, request_id, outcome):
        await self._finish(request_id, kind="UNKNOWN", inputTokens=None, cachedInputTokens=None, outputTokens=None,
                           outcome=outcome)


def _identifiers(body):
    """Role/form/question/segment from the user prompt JSON; never the prompt text itself."""
    result = {"model": body.get("model"), "effort": (body.get("reasoning") or {}).get("effort"),
              "maxOutputTokens": body.get("max_output_tokens"),
              "schema": ((body.get("text") or {}).get("format") or {}).get("name")}
    try:
        prompt = body["input"][1]["content"][0]["text"]
        result["promptSha256"] = sha256(prompt.encode()).hexdigest()
        payload = json.loads(prompt)
    except (KeyError, IndexError, TypeError, ValueError):
        return result
    if isinstance(payload, dict):
        result.update(role=payload.get("role", "TYPO"), form=payload.get("form"),
                      questionRef=payload.get("question_ref"),
                      segmentRef=(payload.get("segment") or {}).get("ref") or payload.get("segment_ref"))
    return result


class CallRecorder:
    """httpx event hooks: `{"request": [recorder.request], "response": [recorder.response]}`."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.started = {}

    async def request(self, request):
        self.started[id(request)] = time.monotonic()

    async def response(self, response):
        request = response.request
        started = self.started.pop(id(request), None)
        await response.aread()
        try:
            body = json.loads(request.content)
        except ValueError:
            body = {}
        row = {"at": time.time(), "path": request.url.path, "status": response.status_code,
               "latencyMs": None if started is None else int((time.monotonic() - started) * 1000),
               **_identifiers(body)}
        try:
            data = response.json()
        except ValueError:
            data = None
        if request.url.path.endswith("input_tokens"):
            row["inputTokens"] = (data or {}).get("input_tokens")
        elif request.url.path.endswith("embeddings"):
            row["usage"] = (data or {}).get("usage")
        elif isinstance(data, dict):
            usage = data.get("usage") or {}
            row.update(responseId=data.get("id"), responseStatus=data.get("status"),
                       incomplete=data.get("incomplete_details"), usage=usage, error=data.get("error"),
                       outputText=_output_text(data))
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")


def _output_text(data):
    if isinstance(data.get("output_text"), str):
        return data["output_text"]
    texts = []
    for item in data.get("output") or []:
        for content in item.get("content") or [] if isinstance(item, dict) else []:
            if isinstance(content, dict) and content.get("type") == "output_text":
                texts.append(content.get("text", ""))
    return "\n".join(texts)
