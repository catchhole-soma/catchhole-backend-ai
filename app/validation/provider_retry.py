"""Validation-only observed one-shot transport and metered, deadline-bounded retries.

Retries live outside metering: every generation attempt reserves its own usage and
failed/unknown usage is retained. No prompt, URL, response body or header dump is logged.
"""
import asyncio
import logging
import math
import random
import re
import time
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime

import httpx

from app.llm.exceptions import LlmInputTokenCountError
from app.validation.contracts import IncompleteInspection

logger = logging.getLogger(__name__)
MAX_ATTEMPTS = 3
MAX_RETRY_DELAY = 60.0
MAX_RECOVERY_SECONDS = 180.0
_CODES = frozenset({
    "rate_limit_exceeded", "slow_down", "server_is_overloaded", "server_error",
    "insufficient_quota", "billing_hard_limit_reached", "invalid_api_key",
    "invalid_request_error", "context_length_exceeded", "model_not_found",
})
_RATE_CODES = {None, "rate_limit_exceeded", "slow_down"}
_REQUEST_ID = re.compile(r"(?:req_[A-Za-z0-9_-]{1,96}|[a-fA-F0-9-]{36})\Z")


class ProviderFailure(RuntimeError):
    def __init__(self, *, operation, status=None, code=None, request_id=None,
                 retry_after=None, retryable=False, kind="HTTP"):
        self.operation, self.status, self.code = operation, status, code
        self.request_id, self.retry_after, self.retryable = request_id, retry_after, retryable
        self.cause = f"MODEL_CALL_HTTP_{status}" if status is not None else f"MODEL_CALL_{kind}"
        super().__init__(self.cause)


def _server_delay(value):
    if value is None:
        return None
    try:
        result = float(value)
    except (ValueError, TypeError):
        try:
            stamp = parsedate_to_datetime(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=UTC)
            result = max(0.0, (stamp - datetime.now(UTC)).total_seconds())
        except (ValueError, TypeError, OverflowError):
            return None
    return result if math.isfinite(result) and result >= 0 else None


def _provider_failure(exc, operation):
    # Only unwrap the exact-count wrapper, never errors from the ledger/lease client.
    for _ in range(4):
        if isinstance(exc, LlmInputTokenCountError) and exc.__cause__ is not None:
            exc = exc.__cause__
        else:
            break
    if isinstance(exc, ProviderFailure):
        return exc
    if isinstance(exc, httpx.HTTPStatusError):
        response = exc.response
        try:
            data = response.json()
            raw_code = data.get("error", {}).get("code") if isinstance(data, dict) else None
        except (ValueError, AttributeError):
            raw_code = "UNRECOGNIZED"
        code = raw_code if isinstance(raw_code, str) and raw_code in _CODES else None
        rid = response.headers.get("x-request-id", "")
        rid = rid if _REQUEST_ID.fullmatch(rid) else None
        status = response.status_code
        quota = code in {"insufficient_quota", "billing_hard_limit_reached"}
        retryable = not quota and (status in {500, 502, 503, 504}
                                  or (status == 429 and (raw_code is None or isinstance(raw_code, str))
                                      and raw_code in _RATE_CODES))
        return ProviderFailure(operation=operation, status=status, code=code, request_id=rid,
                               retry_after=_server_delay(response.headers.get("retry-after")),
                               retryable=retryable)
    if isinstance(exc, (httpx.TimeoutException, httpx.NetworkError, httpx.RemoteProtocolError)):
        return ProviderFailure(operation=operation, retryable=True, kind="TRANSPORT_ERROR")
    return None


class ObservedProvider:
    """Wrap only raw provider calls; does not retry and cannot catch Spring failures."""
    def __init__(self, raw, gate=None):
        self.raw, self.gate = raw, gate

    async def _call(self, operation, method, kwargs):
        if self.gate is not None:
            self.gate.require()
        try:
            return await method(**kwargs)
        except Exception as exc:
            failure = _provider_failure(exc, operation)
            if failure is None:
                raise
            raise failure from None

    async def count_input_tokens(self, **kwargs):
        return await self._call("INPUT_COUNT", self.raw.count_input_tokens, kwargs)

    async def create_text_response(self, **kwargs):
        return await self._call("GENERATION", self.raw.create_text_response, kwargs)


async def invoke_with_retry(operation, budget, role):
    recovery_deadline = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        budget.consume("model")
        remaining = budget.remaining()
        if recovery_deadline is not None:
            remaining = min(remaining, recovery_deadline - time.monotonic())
        try:
            return await asyncio.wait_for(operation(), remaining)
        except Exception as exc:
            # Only failures tagged inside ObservedProvider are eligible. HTTP errors
            # from reservations, settlement, lease or source checks are never retried.
            failure = exc
            for _ in range(4):
                if isinstance(failure, LlmInputTokenCountError) and failure.__cause__:
                    failure = failure.__cause__
                else:
                    break
            if isinstance(failure, IncompleteInspection):
                raise failure from None
            if not isinstance(failure, ProviderFailure):
                raise
            if recovery_deadline is None:
                recovery_deadline = time.monotonic() + MAX_RECOVERY_SECONDS
            delay = failure.retry_after
            if delay is None:
                delay = 2 ** attempt
            delay += random.uniform(0, 0.5)
            allowed = (failure.retryable and attempt < MAX_ATTEMPTS and delay <= MAX_RETRY_DELAY
                       and delay < min(budget.remaining(), recovery_deadline-time.monotonic()))
            logger.warning(
                "validation provider failure role=%s operation=%s attempt=%s status=%s "
                "code=%s request_id=%s retry=%s delay_seconds=%.3f",
                role, failure.operation, attempt, failure.status, failure.code,
                failure.request_id, allowed, delay if allowed else 0,
            )
            if not allowed:
                raise failure from None
            await asyncio.sleep(delay)
