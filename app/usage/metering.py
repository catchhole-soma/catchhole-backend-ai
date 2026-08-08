from collections.abc import Sequence
from functools import lru_cache
import logging
import math
from typing import Protocol, TypeVar
from uuid import UUID, uuid4

import httpx
import tiktoken

from app.embeddings.exceptions import EmbeddingResponseValidationError
from app.embeddings.responses import EmbeddingBatchResponse
from app.llm.exceptions import LlmResponseValidationError
from app.llm.protocols import TextGenerationClient
from app.llm.responses import LlmTextResponse

logger = logging.getLogger(__name__)
TException = TypeVar("TException", bound=BaseException)


# 실제 원장을 소유한 Spring 내부 API에 기대하는 예약·정산·해제 규격
class AiTokenLedgerApi(Protocol):
    def reserve_ai_tokens(
        self,
        request_id: UUID,
        analysis_job_id: UUID,
        purpose: str,
        attempt: int,
        model_name: str,
        reserved_tokens: int,
        lease_token: UUID,
    ) -> None: ...

    def settle_ai_tokens(
        self,
        request_id: UUID,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        outcome: str,
    ) -> None: ...

    def release_ai_tokens(self, request_id: UUID, outcome: str) -> None: ...


class EmbeddingApi(Protocol):
    version: str

    def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse: ...


class MeteredTextGenerationClient:
    """OpenAI 호출마다 최대량을 예약하고 응답 usage로 실제 사용량을 정산한다."""

    def __init__(
        self,
        delegate: TextGenerationClient,
        ledger: AiTokenLedgerApi,
        analysis_job_id: UUID,
        purpose: str,
        default_model: str,
        lease_token: UUID,
    ) -> None:
        self.delegate = delegate
        self.ledger = ledger
        self.analysis_job_id = analysis_job_id
        self.purpose = purpose
        self.default_model = default_model
        self.lease_token = lease_token
        self._attempt = 0

    def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        # attempt는 같은 job·purpose 안에서 발생한 실제 provider 호출 순서를 나타낸다.
        self._attempt += 1
        # 각 provider 호출마다 고유 ID를 발급해 Spring의 멱등 예약·정산 기준으로 사용한다.
        request_id = uuid4()
        effective_model = model or self.default_model
        # 실제 사용량은 호출 후에만 알 수 있으므로 호출 전에는 보수적인 최대량을 예약한다.
        reserved_tokens = _estimate_text_token_upper_bound(
            system_prompt,
            user_prompt,
            effective_model,
            max_output_tokens,
        )
        self.ledger.reserve_ai_tokens(
            request_id=request_id,
            analysis_job_id=self.analysis_job_id,
            purpose=self.purpose,
            attempt=self._attempt,
            model_name=effective_model,
            reserved_tokens=reserved_tokens,
            lease_token=self.lease_token,
        )
        try:
            response = self.delegate.create_text_response(
                system_prompt=system_prompt,
                user_prompt=user_prompt,
                model=model,
                max_output_tokens=max_output_tokens,
                prompt_cache_key=prompt_cache_key,
            )
        except Exception as exc:
            # 오류 응답에도 usage가 있으면 실제량을 보존하고, 없으면 추측하지 않고 예약을 해제한다.
            usage = _usage_from_text_error(exc)
            _finalize_failed_provider_request(self.ledger, request_id, usage)
            raise

        # 성공 응답이라도 usage가 누락되면 실제량을 임의 계산하지 않고 예약을 해제한다.
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
        lease_token: UUID,
    ) -> None:
        self.delegate = delegate
        self.ledger = ledger
        self.analysis_job_id = analysis_job_id
        self.model_name = model_name
        self.lease_token = lease_token
        self.version = delegate.version
        self._attempt = 0

    def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        # 임베딩 batch 한 번을 원장의 요청 한 건으로 기록한다.
        self._attempt += 1
        request_id = uuid4()
        self.ledger.reserve_ai_tokens(
            request_id=request_id,
            analysis_job_id=self.analysis_job_id,
            purpose="CHUNK_EMBEDDING",
            attempt=self._attempt,
            model_name=self.model_name,
            reserved_tokens=_estimate_embedding_token_upper_bound(inputs),
            lease_token=self.lease_token,
        )
        try:
            response = self.delegate.create_embeddings(inputs)
        except Exception as exc:
            # 실패 응답의 usage 유무에 따라 LLM 호출과 동일한 정산 정책을 적용한다.
            usage = _usage_from_embedding_error(exc)
            _finalize_failed_provider_request(self.ledger, request_id, usage)
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
    model: str,
    max_output_tokens: int,
) -> int:
    try:
        encoding = _encoding_for_model(model)
        # 원고에 특수 토큰 표기와 같은 문자열이 있어도 일반 텍스트로 세어 예약이 중단되지 않게 한다.
        content_tokens = len(encoding.encode(system_prompt, disallowed_special=())) + len(
            encoding.encode(user_prompt, disallowed_special=())
        )
    except Exception:
        # 미지원 모델이나 tokenizer cache 장애 시 분석은 계속하되 기존 byte 상한으로 되돌아간다.
        return _estimate_text_token_byte_upper_bound(
            system_prompt,
            user_prompt,
            max_output_tokens,
        )

    # Responses API message framing과 tokenizer 차이를 10% + 256 token으로 흡수하고,
    # 출력은 provider가 허용한 최대량 전체를 예약해 실제 사용량이 quota를 넘지 않게 한다.
    estimated_input_tokens = math.ceil(content_tokens * 1.10) + 256
    return estimated_input_tokens + max_output_tokens


@lru_cache
def _encoding_for_model(model: str):
    # tiktoken 0.13.0은 GPT-5.6 별칭을 아직 알지 못하므로 공식 계열 tokenizer를 명시한다.
    if model.startswith("gpt-5.6"):
        return tiktoken.get_encoding("o200k_base")
    return tiktoken.encoding_for_model(model)


def _estimate_text_token_byte_upper_bound(
    system_prompt: str,
    user_prompt: str,
    max_output_tokens: int,
) -> int:
    prompt_bytes = len(system_prompt.encode("utf-8")) + len(user_prompt.encode("utf-8"))
    return prompt_bytes + max_output_tokens + 512


def _estimate_embedding_token_upper_bound(inputs: Sequence[str]) -> int:
    # 모든 입력의 UTF-8 byte 수에 batch 요청 여유분을 더해 보수적으로 예약한다.
    return sum(len(text.encode("utf-8")) for text in inputs) + 256


def _finalize_failed_provider_request(
    ledger: AiTokenLedgerApi,
    request_id: UUID,
    usage: tuple[int, int, int] | None,
) -> None:
    """원장 정리 실패가 재시도 판단에 필요한 원래 provider 예외를 덮지 않게 한다."""

    try:
        if usage is None:
            ledger.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")
        else:
            ledger.settle_ai_tokens(request_id, *usage, outcome="FAILURE")
    except Exception:
        logger.exception("Failed to finalize AI token ledger request_id=%s", request_id)


def _usage_from_http_error(exc: Exception) -> tuple[int, int, int] | None:
    # provider가 HTTP 오류 body에 usage를 포함한 경우에만 실패 사용량을 정산한다.
    http_error = _find_http_status_error(exc)
    if http_error is None:
        return None
    try:
        payload = http_error.response.json()
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


def _usage_from_embedding_error(exc: Exception) -> tuple[int, int, int] | None:
    """성공 응답 검증 오류와 HTTP 오류에서 provider가 보고한 임베딩 사용량을 찾는다."""

    validation_error = _find_exception(exc, EmbeddingResponseValidationError)
    if validation_error is not None and isinstance(validation_error.input_token_count, int):
        return validation_error.input_token_count, 0, 0
    return _usage_from_http_error(exc)


def _usage_from_text_error(exc: Exception) -> tuple[int, int, int] | None:
    """성공 응답 검증 오류와 HTTP 오류에서 provider가 보고한 텍스트 사용량을 찾는다."""

    validation_error = _find_exception(exc, LlmResponseValidationError)
    if (
        validation_error is not None
        and isinstance(validation_error.input_token_count, int)
        and isinstance(validation_error.output_token_count, int)
    ):
        cached_tokens = validation_error.cached_input_token_count
        return (
            validation_error.input_token_count,
            cached_tokens if isinstance(cached_tokens, int) else 0,
            validation_error.output_token_count,
        )
    return _usage_from_http_error(exc)


def _find_http_status_error(exc: Exception) -> httpx.HTTPStatusError | None:
    """도메인 예외로 감싼 provider HTTP 오류까지 원인 체인에서 찾는다."""

    return _find_exception(exc, httpx.HTTPStatusError)


def _find_exception(
    exc: BaseException,
    exception_type: type[TException],
) -> TException | None:
    """원인 체인에서 요청한 예외 타입을 순환 없이 찾는다."""

    current: BaseException | None = exc
    visited: set[int] = set()
    while current is not None and id(current) not in visited:
        if isinstance(current, exception_type):
            return current
        visited.add(id(current))
        current = current.__cause__ or current.__context__
    return None
