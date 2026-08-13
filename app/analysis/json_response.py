from collections.abc import Callable
import json
from logging import Logger
from typing import TypeVar

from pydantic import BaseModel

from app.analysis.exceptions import LlmExtractionError
from app.llm.protocols import TextGenerationClient

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
) -> ModelT:
    """LLM JSON 객체를 Pydantic 모델로 검증하고 동일 요청 범위에서 재시도한다."""

    last_error: Exception | None = None
    current_user_prompt = user_prompt
    for attempt in range(1, max_attempts + 1):
        try:
            response = await client.create_text_response(
                system_prompt=system_prompt,
                user_prompt=current_user_prompt,
                model=model,
                max_output_tokens=max_output_tokens,
                prompt_cache_key=prompt_cache_key,
            )
            result = response_model.model_validate(parse_json_object(response.text))
            if validate_model is not None:
                validate_model(result)
            return result
        except (TypeError, ValueError) as exc:
            last_error = exc
            if attempt < max_attempts:
                logger.warning(
                    "%s response validation failed. retrying attempt=%s/%s error=%s",
                    operation_name,
                    attempt,
                    max_attempts,
                    compact_error_message(exc),
                )
                if retry_user_prompt_builder is not None:
                    # 매번 최초 입력을 기준으로 피드백을 새로 만들어 실패 문구가
                    # 재시도마다 중첩되지 않게 한다.
                    current_user_prompt = retry_user_prompt_builder(user_prompt, exc)
    raise LlmExtractionError(
        f"{operation_name} failed after {max_attempts} attempts: "
        f"{compact_error_message(last_error)}"
    ) from last_error
