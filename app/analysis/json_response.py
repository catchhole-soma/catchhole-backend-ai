import json
from collections.abc import Callable
from logging import Logger
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from app.analysis.exceptions import (
    ComparisonValidationError,
    LlmExtractionError,
    OrderedInputContextError,
)
from app.llm.exceptions import (
    LlmIncompleteResponseError,
    LlmOutputTruncatedError,
    LlmResponseValidationError,
)
from app.llm.protocols import LlmResponseSchema, TextGenerationClient

ModelT = TypeVar("ModelT", bound=BaseModel)


def parse_json_object(text: str) -> dict:
    """LLM 응답에서 바깥 설명과 Markdown fence를 제거하고 JSON 객체를 읽는다."""

    content = text.strip()
    if content.startswith("```"):
        lines = content.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].startswith("```"):
            lines = lines[:-1]
        content = "\n".join(lines).strip()

    if not content.startswith("{"):
        start = content.find("{")
        end = content.rfind("}")
        if start >= 0 and end >= start:
            content = content[start : end + 1]

    payload = json.loads(content)
    if not isinstance(payload, dict):
        raise TypeError("LLM response must contain a JSON object.")
    return payload


def compact_error_message(exc: Exception | None, max_length: int = 500) -> str:
    if exc is None:
        return "unknown error"
    return (str(exc) or exc.__class__.__name__)[:max_length]


def safe_validation_error_summary(exc: Exception | None) -> str:
    """Provider 값 없이 검증 실패 종류와 코드 위치를 로그와 저장용 오류에 남긴다."""

    if exc is None:
        return "unknown error"
    if not isinstance(exc, ValidationError):
        origin = None
        trace = exc.__traceback__
        while trace is not None:
            module = trace.tb_frame.f_globals.get("__name__", "")
            if module.startswith("app.analysis."):
                code = trace.tb_frame.f_code
                origin = f"{module}.{code.co_qualname}:{trace.tb_lineno}"
            trace = trace.tb_next
        # ponytail: 기존 오류 컬럼을 재사용한다. 위치는 배포 이미지 SHA의 소스와 대조한다.
        name = exc.__class__.__name__
        return f"{name}(origin={origin})" if origin else name
    error_types = sorted(
        {
            error_type
            for error in exc.errors(
                include_url=False,
                include_context=False,
                include_input=False,
            )
            if isinstance(error_type := error.get("type"), str)
        }
    )
    if not error_types:
        return "ValidationError"
    return f"ValidationError(types={','.join(error_types)})"


async def request_validated_model(
    client: TextGenerationClient,
    response_model: type[ModelT],
    system_prompt: str,
    user_prompt: str,
    model: str | None,
    max_output_tokens: int,
    max_attempts: int,
    prompt_cache_key: str,
    operation_name: str,
    logger: Logger,
    validate_model: Callable[[ModelT], None] | None = None,
    retry_user_prompt_builder: Callable[[str, Exception], str] | None = None,
    truncation_retry_max_output_tokens: int | None = None,
    response_schema: LlmResponseSchema | None = None,
    validation_error_summary: Callable[[Exception | None], str] | None = None,
    validation_failure_callback: Callable[[int, Exception, dict | None], None] | None = None,
) -> ModelT:
    """LLM JSON 객체를 Pydantic 모델로 검증하고 동일 요청 범위에서 재시도한다."""

    last_error: Exception | None = None
    current_user_prompt = user_prompt
    current_max_output_tokens = max_output_tokens
    truncation_retry_used = False
    summarize_error = validation_error_summary or safe_validation_error_summary
    for attempt in range(1, max_attempts + 1):
        while True:
            parsed_payload = None
            try:
                response = await client.create_text_response(
                    system_prompt=system_prompt,
                    user_prompt=current_user_prompt,
                    model=model,
                    max_output_tokens=current_max_output_tokens,
                    prompt_cache_key=prompt_cache_key,
                    **({"response_schema": response_schema} if response_schema is not None else {}),
                )
            except LlmOutputTruncatedError as exc:
                can_expand_once = (
                    not truncation_retry_used
                    and truncation_retry_max_output_tokens is not None
                    and truncation_retry_max_output_tokens > current_max_output_tokens
                )
                if not can_expand_once:
                    # 같은 prompt와 같은 cap 재시도는 같은 절단을 반복하므로 즉시 종료한다.
                    raise
                truncation_retry_used = True
                current_max_output_tokens = truncation_retry_max_output_tokens
                logger.warning(
                    "%s output truncated; increasing cap once. "
                    "attempt=%s/%s max_output_tokens=%s next_max_output_tokens=%s "
                    "output_tokens=%s reason=%s",
                    operation_name,
                    attempt,
                    max_attempts,
                    exc.max_output_tokens,
                    current_max_output_tokens,
                    exc.output_token_count,
                    exc.incomplete_reason,
                )
                continue
            except LlmIncompleteResponseError:
                # provider가 완료하지 못한 응답은 JSON/schema 보정으로 회복할 수 없다.
                raise
            except LlmResponseValidationError as exc:
                # Provider가 명시한 응답 형식 오류만 재시도한다. 호출 경계의
                # 입력 상한·quota·lease·예상 밖 오류는 검증 실패로 바꾸지 않는다.
                last_error = exc
            else:
                try:
                    parsed_payload = parse_json_object(response.text)
                    result = response_model.model_validate(parsed_payload)
                    if validate_model is not None:
                        validate_model(result)
                except (
                    OrderedInputContextError,
                    LlmIncompleteResponseError,
                    LlmOutputTruncatedError,
                ):
                    # 고정 입력/실행 오류는 도메인 응답 보정으로 회복되지 않는다.
                    raise
                except (TypeError, ValueError, ComparisonValidationError) as exc:
                    last_error = exc
                else:
                    return result
            if validation_failure_callback is not None:
                try:
                    validation_failure_callback(attempt, last_error, parsed_payload)
                except Exception:  # noqa: BLE001 - optional diagnostics cannot replace the real failure.
                    logger.warning("%s validation diagnostics unavailable. attempt=%s", operation_name, attempt)
            if attempt < max_attempts:
                logger.warning(
                    "%s response validation failed. retrying attempt=%s/%s error=%s",
                    operation_name,
                    attempt,
                    max_attempts,
                    summarize_error(last_error),
                )
                if retry_user_prompt_builder is not None:
                    # 매번 최초 입력을 기준으로 피드백을 새로 만들어 실패 문구가
                    # 재시도마다 중첩되지 않게 한다.
                    current_user_prompt = retry_user_prompt_builder(user_prompt, last_error)
            break
    error_type = (
        ComparisonValidationError
        if isinstance(last_error, ComparisonValidationError)
        or "comparison" in operation_name.casefold()
        else LlmExtractionError
    )
    sanitized_cause = (
        LlmResponseValidationError(summarize_error(last_error))
        if isinstance(last_error, LlmResponseValidationError)
        else None
    )
    raise error_type(
        f"{operation_name} failed after {max_attempts} attempts: "
        f"{summarize_error(last_error)}"
    ) from sanitized_cause
