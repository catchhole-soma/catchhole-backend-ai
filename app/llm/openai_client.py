import json
import logging

import httpx

from app.core.config import Settings, get_settings
from app.llm.exceptions import (
    LlmIncompleteResponseError,
    LlmOutputTruncatedError,
    LlmResponseValidationError,
)
from app.llm.responses import LlmTextResponse

logger = logging.getLogger(__name__)


# "OpenAI Responses API 호출만" 담당하는 client
class OpenAIResponsesClient:
    def __init__(
        self,
        api_key: str,  # OpenAi 키
        model: str,  # 기본 모델명
        responses_api_url: str,  # OpenAI Responses API 주소
        reasoning_effort: str | None = None,  # GPT-5.6 추론 강도
        http_client: httpx.AsyncClient | None = None,  # 실제 HTTP 요청 도구
    ) -> None:
        self.api_key = api_key
        # 기본 모델명, 호출할 때 model을 따로 넘기면 그 값이 우선
        self.model = model
        # 기본값은 https://api.openai.com/v1/responses, 테스트에서는 fake URL을 넣을 수 있음
        self.responses_api_url = responses_api_url
        self.reasoning_effort = reasoning_effort
        # 실제 HTTP 요청을 보내는 도구, 테스트에서는 MockTransport가 들어간 client를 주입
        self.http_client = http_client or httpx.AsyncClient(timeout=120)

    # .env에서 읽은 설정값으로 client를 만드는 생성 보조 함수
    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "OpenAIResponsesClient":
        settings = settings or get_settings()
        return cls(
            api_key=settings.llm_api_key,
            model=settings.llm_model,
            responses_api_url=settings.openai_responses_api_url,
            reasoning_effort=settings.llm_reasoning_effort,
        )

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        if not self.api_key:
            raise ValueError("LLM_API_KEY is required.")

        # 실제 OpenAi API 요청을 보내는 부분
        # system_prompt는 역할/규칙, user_prompt는 실제 청크 입력을 담음
        effective_model = model or self.model
        request_body = {
            # 호출별 model이 있으면 그걸 쓰고, 없으면 Settings의 기본 모델을 쓴다.
            "model": effective_model,
            "input": [
                {
                    "role": "system",
                    "content": [{"type": "input_text", "text": system_prompt}],
                },
                {
                    "role": "user",
                    "content": [{"type": "input_text", "text": user_prompt}],
                },
            ],
            "max_output_tokens": max_output_tokens,
        }
        # 같은 정적 prefix를 공유하는 요청만 동일한 key를 사용해 cache routing을 돕는다.
        if prompt_cache_key is not None:
            request_body["prompt_cache_key"] = prompt_cache_key
        effective_reasoning_effort = _resolve_reasoning_effort(
            effective_model,
            self.reasoning_effort,
        )
        if effective_reasoning_effort is not None:
            request_body["reasoning"] = {"effort": effective_reasoning_effort}

        response = await self.http_client.post(
            self.responses_api_url,
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=request_body,
        )
        # 4xx/5xx 응답이면 httpx.HTTPStatusError를 발생
        response.raise_for_status()
        # OpenAI 응답 JSON을 dict로 바꾼 뒤, 필요한 text와 token usage만 내부 schema로 정리
        payload = response.json()
        raw_usage = payload.get("usage") or {}
        usage = raw_usage if isinstance(raw_usage, dict) else {}
        input_details = usage.get("input_tokens_details") or {}
        input_token_count = usage.get("input_tokens")
        cached_input_token_count = (
            input_details.get("cached_tokens") if isinstance(input_details, dict) else None
        )
        output_token_count = usage.get("output_tokens")
        normalized_input_tokens = input_token_count if isinstance(input_token_count, int) else None
        normalized_cached_input_tokens = (
            cached_input_token_count if isinstance(cached_input_token_count, int) else None
        )
        normalized_output_tokens = (
            output_token_count if isinstance(output_token_count, int) else None
        )
        response_status = payload.get("status")
        incomplete_details = payload.get("incomplete_details") or {}
        incomplete_reason = (
            incomplete_details.get("reason") if isinstance(incomplete_details, dict) else None
        )
        logger.debug(
            "OpenAI token usage received. input_tokens=%s output_tokens=%s "
            "cached_tokens_present=%s cached_tokens=%s input_tokens_details=%s",
            usage.get("input_tokens"),
            usage.get("output_tokens"),
            isinstance(input_details, dict) and "cached_tokens" in input_details,
            input_details.get("cached_tokens") if isinstance(input_details, dict) else None,
            input_details,
        )
        if response_status != "completed":
            if response_status == "incomplete" and _is_output_limit_reason(incomplete_reason):
                raise LlmOutputTruncatedError(
                    "OpenAI response was truncated by the output token limit.",
                    incomplete_reason=incomplete_reason,
                    max_output_tokens=max_output_tokens,
                    input_token_count=normalized_input_tokens,
                    cached_input_token_count=normalized_cached_input_tokens,
                    output_token_count=normalized_output_tokens,
                )
            raise LlmIncompleteResponseError(
                "OpenAI response did not reach completed status.",
                input_token_count=normalized_input_tokens,
                cached_input_token_count=normalized_cached_input_tokens,
                output_token_count=normalized_output_tokens,
            )
        try:
            output_text = self._extract_output_text(payload)
        except (AttributeError, TypeError, ValueError) as exc:
            # HTTP 200 응답 구조가 잘못되어도 이미 과금된 provider usage는 정산할 수 있게 보존한다.
            raise LlmResponseValidationError(
                str(exc),
                input_token_count=normalized_input_tokens,
                cached_input_token_count=normalized_cached_input_tokens,
                output_token_count=normalized_output_tokens,
            ) from exc
        if (
            normalized_output_tokens is not None
            and normalized_output_tokens >= max_output_tokens
            and not _contains_complete_json_object(output_text)
        ):
            raise LlmOutputTruncatedError(
                "OpenAI output reached the token limit before a complete JSON object was produced.",
                incomplete_reason="output_token_limit_reached",
                max_output_tokens=max_output_tokens,
                input_token_count=normalized_input_tokens,
                cached_input_token_count=normalized_cached_input_tokens,
                output_token_count=normalized_output_tokens,
            )
        return LlmTextResponse(
            text=output_text,
            input_token_count=input_token_count,
            cached_input_token_count=cached_input_token_count,
            output_token_count=output_token_count,
            raw_response=payload,
        )

    async def aclose(self) -> None:
        await self.http_client.aclose()

    def _extract_output_text(self, payload: dict) -> str:
        # Responses API가 output_text를 바로 주는 경우 먼저 사용
        direct_output_text = payload.get("output_text")
        if direct_output_text:
            if not isinstance(direct_output_text, str):
                raise TypeError("OpenAI output_text must be a string.")
            return direct_output_text

        # 일부 응답 형태는 output[].content[] 안에 output_text가 들어올 수 있어서 fallback으로 처리
        output_texts: list[str] = []
        for output_item in payload.get("output", []):
            for content in output_item.get("content", []):
                if content.get("type") == "output_text" and content.get("text"):
                    output_texts.append(content["text"])

        return "\n".join(output_texts).strip()


def _supports_reasoning(model: str) -> bool:
    """Responses API에서 reasoning 설정을 받는 현재 사용 모델 계열만 허용한다."""

    return model.startswith(("gpt-5", "o1", "o3", "o4"))


def _resolve_reasoning_effort(model: str, configured_effort: str | None) -> str | None:
    """모델 전용 기본값이 호환되지 않는 override 요청에는 추론 강도를 상속하지 않는다."""

    if configured_effort is None or not _supports_reasoning(model):
        return None
    if configured_effort == "none" and not model.startswith("gpt-5.6"):
        return None
    return configured_effort


def _is_output_limit_reason(reason: object) -> bool:
    return isinstance(reason, str) and reason.strip().casefold() in {
        "max_tokens",
        "max_output_tokens",
        "max_completion_tokens",
    }


def _contains_complete_json_object(text: str) -> bool:
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
    try:
        payload = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        return False
    return isinstance(payload, dict)
