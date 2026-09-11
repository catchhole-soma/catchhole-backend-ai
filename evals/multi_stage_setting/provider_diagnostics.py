"""Safe provider diagnostics; raw requests, bodies and free-form messages stay private."""

import hashlib
import json
import re
from typing import Any

import httpx

ERROR_CODES = {
    "invalid_api_key",
    "incorrect_api_key",
    "invalid_request_error",
    "invalid_value",
    "unsupported_parameter",
    "unsupported_value",
    "model_not_found",
    "permission_denied",
    "insufficient_quota",
    "rate_limit_exceeded",
    "tokens",
    "requests",
    "billing_hard_limit_reached",
    "billing_not_active",
    "account_deactivated",
    "context_length_exceeded",
    "server_error",
    "internal_server_error",
    "overloaded",
    "service_unavailable",
    "timeout",
    "content_filter",
    "invalid_json_schema",
    "invalid_prompt",
    "image_parse_error",
    "too_many_requests",
    "authentication_error",
    "permission_error",
    "api_error",
    "slow_down",
    "server_overloaded",
    "engine_overloaded",
}
# Only fixed summaries of provider messages are published, never interpolated text.
MESSAGE_SUMMARIES = {
    "slow down": "Provider requested slower requests.",
    "overloaded": "Provider reported overload.",
    "temporarily unavailable": "Provider reported temporary unavailability.",
    "service unavailable": "Provider reported temporary unavailability.",
    "timed out": "Provider reported a timeout.",
    "timeout": "Provider reported a timeout.",
    "rate limit": "Provider reported a rate limit.",
    "maximum context length": "Provider reported a context length limit.",
    "invalid schema": "Provider reported an invalid schema.",
    "internal server error": "Provider reported an internal server error.",
}
NETWORK_EXCEPTIONS = {
    cls.__name__: cls for cls in (
        httpx.ConnectTimeout, httpx.ReadTimeout, httpx.WriteTimeout, httpx.PoolTimeout,
        httpx.ConnectError, httpx.ReadError, httpx.WriteError, httpx.CloseError,
        httpx.RemoteProtocolError, httpx.LocalProtocolError,
    )
}
COUNT_FIELDS = {
    "elapsed_ms", "max_output_tokens", "prompt_chars", "prompt_bytes", "schema_bytes",
}
PURPOSES = {
    "setting-extraction": "CHARACTER_EXTRACTION",
    "subject-resolution": "CHARACTER_SUBJECT_RESOLUTION",
    "character-fact-comparison": "CHARACTER_COMPARISON",
    "character-fact-comparison-batch": "CHARACTER_COMPARISON",
    "world-setting-extraction": "WORLD_EXTRACTION",
    "world-setting-subject-resolution": "WORLD_SUBJECT_RESOLUTION",
    "world-setting-comparison": "WORLD_COMPARISON",
    "world-setting-comparison-batch": "WORLD_COMPARISON",
}
PARAMETERS = {
    "model",
    "input",
    "instructions",
    "max_output_tokens",
    "reasoning",
    "reasoning.effort",
    "text",
    "text.format",
    "text.format.schema",
    "response_format",
    "response_format.schema",
    "temperature",
    "top_p",
    "tools",
    "tool_choice",
    "store",
    "prompt_cache_key",
}


def sanitize_provider_details(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    result = {}
    for key in (
        "model",
        "purpose",
        "http_status",
        "provider_error_code",
        "provider_error_type",
        "parameter",
        "request_id",
        "response_status",
        "incomplete_reason",
        "network_exception",
        "message_summary",
        "elapsed_ms",
        "max_output_tokens",
        "prompt_chars",
        "prompt_bytes",
        "schema_bytes",
        "input_fingerprint",
        "store_responses",
        "response_id",
    ):
        parts = key.split("_")
        alias = parts[0] + "".join(part.title() for part in parts[1:])
        item = value.get(key, value.get(alias))
        if item is None:
            continue
        if key == "store_responses":
            if type(item) is bool:
                result[key] = item
        elif key in COUNT_FIELDS:
            if type(item) is int and 0 <= item <= 10**12:
                result[key] = item
        elif key == "http_status":
            if type(item) is int and 100 <= item <= 599:
                result[key] = item
        elif not isinstance(item, str):
            continue
        elif key == "model":
            if (
                re.fullmatch(r"(?:gpt-[0-9][a-z0-9.-]*|o[0-9][a-z0-9.-]*)", item)
                and len(item) <= 80
            ):
                result[key] = item
        elif key == "network_exception" and item in NETWORK_EXCEPTIONS:
            result[key] = item
        elif key == "message_summary":
            if item in MESSAGE_SUMMARIES.values() or item == "Unrecognized provider message (withheld).":
                result[key] = item
        elif key == "input_fingerprint" and re.fullmatch(r"[0-9a-f]{64}", item):
            result[key] = item
        elif key == "request_id":
            if re.fullmatch(r"req_[a-zA-Z0-9_-]{1,128}|[0-9a-fA-F-]{36}", item):
                result[key] = item
        elif key == "response_id" and re.fullmatch(r"resp_[a-zA-Z0-9_-]{1,128}", item):
            result[key] = item
        elif key in {"provider_error_code", "provider_error_type"}:
            result[key] = item if item in ERROR_CODES else "UNRECOGNIZED"
        elif key == "parameter":
            result[key] = item if item in PARAMETERS else "UNRECOGNIZED"
        elif key == "purpose" and item in PURPOSES.values():
            result[key] = item
        elif key == "response_status":
            result[key] = (
                item
                if item
                in {
                    "completed",
                    "failed",
                    "incomplete",
                    "cancelled",
                    "queued",
                    "in_progress",
                }
                else "UNRECOGNIZED"
            )
        elif key == "incomplete_reason":
            result[key] = (
                item
                if item
                in {
                    "max_output_tokens",
                    "max_tokens",
                    "content_filter",
                }
                else "UNRECOGNIZED"
            )
    return result


def request_size_details(system_prompt: str, user_prompt: str, schema: Any) -> dict[str, Any]:
    """Describe size and compare identical inputs without logging their contents."""
    schema_json = json.dumps(schema, ensure_ascii=False, sort_keys=True)
    fingerprint_input = json.dumps(
        [system_prompt, user_prompt, schema], ensure_ascii=False, sort_keys=True,
    ).encode("utf-8")
    return {
        "prompt_chars": len(system_prompt) + len(user_prompt),
        "prompt_bytes": len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8")),
        "schema_bytes": len(schema_json.encode("utf-8")) if schema is not None else 0,
        "input_fingerprint": hashlib.sha256(fingerprint_input).hexdigest(),
    }


def provider_failure_details(
    exc: BaseException,
    *,
    model: str | None = None,
    prompt_cache_key: str | None = None,
) -> dict[str, Any]:
    """Read safe metadata from the actual failed call, also through exception wrapping."""
    current = exc
    visited = set()
    details: dict[str, Any] = {}
    while current is not None and id(current) not in visited:
        visited.add(id(current))
        attached = getattr(current, "evaluation_provider_details", None)
        if isinstance(attached, dict):
            details = sanitize_provider_details(attached)
            break
        for name, exception_type in NETWORK_EXCEPTIONS.items():
            if isinstance(current, exception_type):
                details.setdefault("network_exception", name)
                break
        response = getattr(current, "response", None)
        if isinstance(response, httpx.Response):
            details["http_status"] = response.status_code
            details["request_id"] = response.headers.get("x-request-id")
            try:
                payload = response.json()
            except (ValueError, httpx.ResponseNotRead):
                payload = {}
            if isinstance(payload, dict):
                error = payload.get("error")
                if isinstance(error, dict):
                    details.update(
                        provider_error_code=error.get("code"),
                        provider_error_type=error.get("type"),
                        parameter=error.get("param"),
                    )
                    message = error.get("message")
                    if isinstance(message, str):
                        details["message_summary"] = next(
                            (summary for phrase, summary in MESSAGE_SUMMARIES.items()
                             if phrase in message.lower()),
                            "Unrecognized provider message (withheld).",
                        )
                details["response_status"] = payload.get("status")
                incomplete = payload.get("incomplete_details")
                if isinstance(incomplete, dict):
                    details["incomplete_reason"] = incomplete.get("reason")
            break
        current = current.__cause__ or current.__context__
    if model is not None:
        details["model"] = model
    if prompt_cache_key is not None:
        details["purpose"] = PURPOSES.get(prompt_cache_key.partition(":")[0])
    reason = getattr(exc, "incomplete_reason", None)
    if reason is not None:
        details.setdefault("incomplete_reason", reason)
    return sanitize_provider_details(details)
