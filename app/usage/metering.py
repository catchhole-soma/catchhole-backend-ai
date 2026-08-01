from collections.abc import Sequence
from typing import Protocol
from uuid import UUID, uuid4

import httpx

from app.embeddings.responses import EmbeddingBatchResponse
from app.llm.responses import LlmTextResponse


class AiTokenLedgerApi(Protocol):
    def reserve_ai_tokens(
        self,
        request_id: UUID,
        analysis_job_id: UUID,
        purpose: str,
        attempt: int,
        model_name: str,
        reserved_tokens: int,
    ) -> None:
        pass

    def settle_ai_tokens(
        self,
        request_id: UUID,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        outcome: str,
    ) -> None:
        pass

    def release_ai_tokens(self, request_id: UUID, outcome: str) -> None:
        pass


class TextGenerationApi(Protocol):
    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
    ) -> LlmTextResponse:
        pass


class EmbeddingApi(Protocol):
    version: str

    def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        pass


class MeteredTextGenerationClient:
    """OpenAI 호출마다 최대량을 예약하고 응답 usage로 실제 사용량을 정산한다."""

    def __init__(
        self,
        delegate: TextGenerationApi,
        ledger: AiTokenLedgerApi,
        analysis_job_id: UUID,
        purpose: str,
        default_model: str,
    ) -> None:
        self.delegate = delegate
        self.ledger = ledger
        self.analysis_job_id = analysis_job_id
        self.purpose = purpose
        self.default_model = default_model
        self._attempt = 0

    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
    ) -> LlmTextResponse:
        self._attempt += 1
        request_id = uuid4()
        effective_model = model or self.default_model
        reserved_tokens = _estimate_text_token_upper_bound(
            system_prompt,
            user_prompt,
            max_output_tokens,
        )
        self.ledger.reserve_ai_tokens(
            request_id=request_id,
            analysis_job_id=self.analysis_job_id,
            purpose=self.purpose,
            attempt=self._attempt,
            model_name=effective_model,
            reserved_tokens=reserved_tokens,
        )
        try:
            response = self.delegate.create_text_response(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                max_output_tokens=max_output_tokens,
            )
        except Exception as exc:
            usage = _usage_from_http_error(exc)
            if usage is None:
                self.ledger.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")
            else:
                self.ledger.settle_ai_tokens(request_id, *usage, outcome="FAILURE")
            raise

        if response.input_token_count is None or response.output_token_count is None:
            self.ledger.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")
        else:
            self.ledger.settle_ai_tokens(
                request_id=request_id,
                input_tokens=response.input_token_count,
                cached_input_tokens=response.cached_input_token_count or 0,
                output_tokens=response.output_token_count,
                outcome="SUCCESS",
            )
        return response


class MeteredEmbeddingClient:
    """한 배치 임베딩 요청도 LLM 요청과 같은 원장 계약으로 정산한다."""

    def __init__(
        self,
        delegate: EmbeddingApi,
        ledger: AiTokenLedgerApi,
        analysis_job_id: UUID,
        model_name: str,
    ) -> None:
        self.delegate = delegate
        self.ledger = ledger
        self.analysis_job_id = analysis_job_id
        self.model_name = model_name
        self.version = delegate.version
        self._attempt = 0

    def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        self._attempt += 1
        request_id = uuid4()
        self.ledger.reserve_ai_tokens(
            request_id=request_id,
            analysis_job_id=self.analysis_job_id,
            purpose="CHUNK_EMBEDDING",
            attempt=self._attempt,
            model_name=self.model_name,
            reserved_tokens=_estimate_embedding_token_upper_bound(inputs),
        )
        try:
            response = self.delegate.create_embeddings(inputs)
        except Exception as exc:
            usage = _usage_from_http_error(exc)
            if usage is None:
                self.ledger.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")
            else:
                self.ledger.settle_ai_tokens(request_id, *usage, outcome="FAILURE")
            raise

        if response.input_token_count is None:
            self.ledger.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")
        else:
            self.ledger.settle_ai_tokens(
                request_id=request_id,
                input_tokens=response.input_token_count,
                cached_input_tokens=0,
                output_tokens=0,
                outcome="SUCCESS",
            )
        return response


def _estimate_text_token_upper_bound(
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
) -> int:
    # UTF-8 byte 수는 한글을 포함한 BPE token 수보다 보수적으로 크다.
    prompt_bytes = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
    return prompt_bytes + max_output_tokens + 512


def _estimate_embedding_token_upper_bound(inputs: Sequence[str]) -> int:
    return sum(len(text.encode("utf-8")) for text in inputs) + 256


def _usage_from_http_error(exc: Exception) -> tuple[int, int, int] | None:
    if not isinstance(exc, httpx.HTTPStatusError):
        return None
    try:
        payload = exc.response.json()
    except ValueError:
        return None
    usage = payload.get("usage") if isinstance(payload, dict) else None
    if not isinstance(usage, dict):
        return None
    input_tokens = usage.get("input_tokens", usage.get("prompt_tokens"))
    output_tokens = usage.get("output_tokens", 0)
    if not isinstance(input_tokens, int) or not isinstance(output_tokens, int):
        return None
    input_details = usage.get("input_tokens_details") or {}
    cached_tokens = input_details.get("cached_tokens", 0) if isinstance(input_details, dict) else 0
    return input_tokens, cached_tokens if isinstance(cached_tokens, int) else 0, output_tokens
