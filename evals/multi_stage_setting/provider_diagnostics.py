"""Allowlisted provider metadata; never persist requests, response bodies, or error messages."""

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
    ):
        parts = key.split("_")
        alias = parts[0] + "".join(part.title() for part in parts[1:])
        item = value.get(key, value.get(alias))
        if item is None:
            continue
        if key == "http_status":
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
        elif key == "request_id":
            if re.fullmatch(r"req_[a-zA-Z0-9_-]{1,128}|[0-9a-fA-F-]{36}", item):
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
