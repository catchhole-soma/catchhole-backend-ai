import asyncio
import logging
import math
import random
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from functools import lru_cache
from typing import Protocol, TypeVar
from uuid import UUID, uuid4

import httpx
import tiktoken

from app.embeddings.exceptions import (
    EmbeddingResponseValidationError,
    RecoverableEmbeddingProviderError,
)
from app.embeddings.responses import EmbeddingBatchResponse
from app.llm.exceptions import LlmOutputTruncatedError, LlmResponseValidationError
from app.llm.protocols import TextGenerationClient
from app.llm.responses import LlmTextResponse

logger = logging.getLogger(__name__)
TException = TypeVar("TException", bound=BaseException)
AsyncSleeper = Callable[[float], Awaitable[None]]
RandomSource = Callable[[], float]
_detached_ledger_tasks: set[asyncio.Task[None]] = set()

NON_RETRYABLE_429_CODES = frozenset(
    {
        "billing_hard_limit_reached",
        "billing_not_active",
        "insufficient_quota",
        "quota_exceeded",
        "usage_limit_reached",
    }
)


# 실제 원장을 소유한 Spring 내부 API에 기대하는 예약·정산·해제 규격
class AiTokenLedgerApi(Protocol):
    async def reserve_ai_tokens(
        self,
        request_id: UUID,
        analysis_job_id: UUID,
        purpose: str,
        attempt: int,
        model_name: str,
        reserved_tokens: int,
        lease_token: UUID,
    ) -> None: ...

    async def settle_ai_tokens(
        self,
        request_id: UUID,
        input_tokens: int,
        cached_input_tokens: int,
        output_tokens: int,
        outcome: str,
    ) -> None: ...

    async def release_ai_tokens(self, request_id: UUID, outcome: str) -> None: ...


class EmbeddingApi(Protocol):
    version: str

    async def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse: ...


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
        request_semaphore: asyncio.Semaphore | None = None,
        max_retries: int = 3,
        retry_base_seconds: float = 2.0,
        sleeper: AsyncSleeper = asyncio.sleep,
        random_source: RandomSource = random.random,
        jitter_max_seconds: float = 1.0,
    ) -> None:
        _validate_retry_configuration(max_retries, retry_base_seconds, jitter_max_seconds)
        self.delegate = delegate
        self.ledger = ledger
        self.analysis_job_id = analysis_job_id
        self.purpose = purpose
        self.default_model = default_model
        self.lease_token = lease_token
        self.request_semaphore = request_semaphore
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.sleeper = sleeper
        self.random_source = random_source
        self.jitter_max_seconds = jitter_max_seconds
        self._attempt = 0

    async def create_text_response(
        self,
        system_prompt: str,
        user_prompt: str,
        model: str | None = None,
        max_output_tokens: int = 1500,
        prompt_cache_key: str | None = None,
    ) -> LlmTextResponse:
        effective_model = model or self.default_model
        reserved_tokens = _estimate_text_token_upper_bound(
            system_prompt,
            user_prompt,
            effective_model,
            max_output_tokens,
        )
        for retry_index in range(self.max_retries + 1):
            async with _optional_semaphore(self.request_semaphore):
                self._attempt += 1
                request_id = uuid4()
                await _reserve_tokens_cancellation_safe(
                    ledger=self.ledger,
                    request_id=request_id,
                    analysis_job_id=self.analysis_job_id,
                    purpose=self.purpose,
                    attempt=self._attempt,
                    model_name=effective_model,
                    reserved_tokens=reserved_tokens,
                    lease_token=self.lease_token,
                )
                try:
                    response = await self.delegate.create_text_response(
                        system_prompt=system_prompt,
                        user_prompt=user_prompt,
                        model=model,
                        max_output_tokens=max_output_tokens,
                        prompt_cache_key=prompt_cache_key,
                    )
                except asyncio.CancelledError:
                    _detach_failed_provider_finalization(self.ledger, request_id)
                    raise
                except Exception as exc:
                    usage = _usage_from_text_error(exc)
                    if isinstance(exc, LlmOutputTruncatedError):
                        logger.warning(
                            "LLM output truncated. purpose=%s attempt=%s "
                            "max_output_tokens=%s output_tokens=%s reason=%s",
                            self.purpose,
                            self._attempt,
                            exc.max_output_tokens,
                            exc.output_token_count,
                            exc.incomplete_reason,
                        )
                    await _finalize_failed_provider_request(
                        self.ledger,
                        request_id,
                        usage,
                    )
                    if retry_index >= self.max_retries or not _is_retryable_provider_error(exc):
                        raise
                    retry_delay = _provider_retry_delay(
                        exc=exc,
                        retry_index=retry_index,
                        retry_base_seconds=self.retry_base_seconds,
                        random_source=self.random_source,
                        jitter_max_seconds=self.jitter_max_seconds,
                    )
                else:
                    await _finalize_successful_text_request(
                        self.ledger,
                        request_id,
                        response,
                    )
                    return response

            await self.sleeper(retry_delay)

        raise AssertionError("Provider retry loop terminated unexpectedly.")


class MeteredEmbeddingClient:
    """한 배치 임베딩 요청도 LLM 요청과 같은 원장 계약으로 정산한다."""

    def __init__(
        self,
        delegate: EmbeddingApi,
        ledger: AiTokenLedgerApi,
        analysis_job_id: UUID,
        model_name: str,
        lease_token: UUID,
        request_semaphore: asyncio.Semaphore | None = None,
        max_retries: int = 3,
        retry_base_seconds: float = 2.0,
        sleeper: AsyncSleeper = asyncio.sleep,
        random_source: RandomSource = random.random,
        jitter_max_seconds: float = 1.0,
    ) -> None:
        _validate_retry_configuration(max_retries, retry_base_seconds, jitter_max_seconds)
        self.delegate = delegate
        self.ledger = ledger
        self.analysis_job_id = analysis_job_id
        self.model_name = model_name
        self.lease_token = lease_token
        self.request_semaphore = request_semaphore
        self.max_retries = max_retries
        self.retry_base_seconds = retry_base_seconds
        self.sleeper = sleeper
        self.random_source = random_source
        self.jitter_max_seconds = jitter_max_seconds
        self.version = delegate.version
        self._attempt = 0

    async def create_embeddings(self, inputs: list[str]) -> EmbeddingBatchResponse:
        reserved_tokens = _estimate_embedding_token_upper_bound(inputs)
        for retry_index in range(self.max_retries + 1):
            async with _optional_semaphore(self.request_semaphore):
                self._attempt += 1
                request_id = uuid4()
                await _reserve_tokens_cancellation_safe(
                    ledger=self.ledger,
                    request_id=request_id,
                    analysis_job_id=self.analysis_job_id,
                    purpose="CHUNK_EMBEDDING",
                    attempt=self._attempt,
                    model_name=self.model_name,
                    reserved_tokens=reserved_tokens,
                    lease_token=self.lease_token,
                )
                try:
                    response = await self.delegate.create_embeddings(inputs)
                except asyncio.CancelledError:
                    _detach_failed_provider_finalization(self.ledger, request_id)
                    raise
                except Exception as exc:
                    usage = _usage_from_embedding_error(exc)
                    await _finalize_failed_provider_request(
                        self.ledger,
                        request_id,
                        usage,
                    )
                    if retry_index >= self.max_retries or not _is_retryable_provider_error(exc):
                        raise
                    retry_delay = _provider_retry_delay(
                        exc=exc,
                        retry_index=retry_index,
                        retry_base_seconds=self.retry_base_seconds,
                        random_source=self.random_source,
                        jitter_max_seconds=self.jitter_max_seconds,
                    )
                else:
                    await _finalize_successful_embedding_request(
                        self.ledger,
                        request_id,
                        response,
                    )
                    return response

            await self.sleeper(retry_delay)

        raise AssertionError("Provider retry loop terminated unexpectedly.")


@asynccontextmanager
async def _optional_semaphore(
    semaphore: asyncio.Semaphore | None,
) -> AsyncIterator[None]:
    if semaphore is None:
        yield
        return
    async with semaphore:
        yield


def _validate_retry_configuration(
    max_retries: int,
    retry_base_seconds: float,
    jitter_max_seconds: float,
) -> None:
    if max_retries < 0:
        raise ValueError("max_retries must be at least zero.")
    if retry_base_seconds < 0:
        raise ValueError("retry_base_seconds must be at least zero.")
    if jitter_max_seconds < 0:
        raise ValueError("jitter_max_seconds must be at least zero.")


async def _reserve_tokens_cancellation_safe(
    ledger: AiTokenLedgerApi,
    request_id: UUID,
    analysis_job_id: UUID,
    purpose: str,
    attempt: int,
    model_name: str,
    reserved_tokens: int,
    lease_token: UUID,
) -> None:
    """Reserve tokens without delaying owning Job cancellation."""

    reservation_task = asyncio.create_task(
        ledger.reserve_ai_tokens(
            request_id=request_id,
            analysis_job_id=analysis_job_id,
            purpose=purpose,
            attempt=attempt,
            model_name=model_name,
            reserved_tokens=reserved_tokens,
            lease_token=lease_token,
        )
    )
    try:
        await asyncio.shield(reservation_task)
    except asyncio.CancelledError:
        cleanup_task = asyncio.create_task(
            _cleanup_cancelled_reservation(ledger, reservation_task, request_id)
        )
        _detach_ledger_task(
            cleanup_task,
            f"AI token reservation cleanup failed. request_id={request_id}",
        )
        raise


async def _complete_ledger_call_on_cancellation(awaitable: Awaitable[None]) -> None:
    """Observe ledger completion without delaying owning Job cancellation."""

    ledger_task = asyncio.create_task(awaitable)
    try:
        await asyncio.shield(ledger_task)
    except asyncio.CancelledError:
        _detach_ledger_task(ledger_task, "Detached AI token ledger call failed.")
        raise


async def _cleanup_cancelled_reservation(
    ledger: AiTokenLedgerApi,
    reservation_task: asyncio.Task[None],
    request_id: UUID,
) -> None:
    await reservation_task
    await _finalize_failed_provider_request(ledger, request_id, usage=None)


def _detach_ledger_task(task: asyncio.Task[None], failure_message: str) -> None:
    _detached_ledger_tasks.add(task)

    def consume_result(completed: asyncio.Task[None]) -> None:
        _detached_ledger_tasks.discard(completed)
        if completed.cancelled():
            return
        try:
            completed.result()
        except Exception:
            logger.exception(failure_message)

    task.add_done_callback(consume_result)


def _detach_failed_provider_finalization(
    ledger: AiTokenLedgerApi,
    request_id: UUID,
) -> None:
    cleanup_task = asyncio.create_task(
        _finalize_failed_provider_request(ledger, request_id, usage=None)
    )
    _detach_ledger_task(
        cleanup_task,
        f"Cancelled provider request cleanup failed. request_id={request_id}",
    )


async def _finalize_successful_text_request(
    ledger: AiTokenLedgerApi,
    request_id: UUID,
    response: LlmTextResponse,
) -> None:
    if response.input_token_count is None or response.output_token_count is None:
        await _complete_ledger_call_on_cancellation(
            ledger.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")
        )
        return
    await _complete_ledger_call_on_cancellation(
        ledger.settle_ai_tokens(
            request_id=request_id,
            input_tokens=response.input_token_count,
            cached_input_tokens=response.cached_input_token_count or 0,
            output_tokens=response.output_token_count,
            outcome="SUCCESS",
        )
    )


async def _finalize_successful_embedding_request(
    ledger: AiTokenLedgerApi,
    request_id: UUID,
    response: EmbeddingBatchResponse,
) -> None:
    if response.input_token_count is None:
        await _complete_ledger_call_on_cancellation(
            ledger.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")
        )
        return
    await _complete_ledger_call_on_cancellation(
        ledger.settle_ai_tokens(
            request_id=request_id,
            input_tokens=response.input_token_count,
            cached_input_tokens=0,
            output_tokens=0,
            outcome="SUCCESS",
        )
    )


def _is_retryable_provider_error(exc: Exception) -> bool:
    http_error = _find_http_status_error(exc)
    if http_error is not None:
        status_code = http_error.response.status_code
        if status_code == 429:
            return not _is_non_retryable_429(http_error)
        if status_code == 408:
            return True
        if status_code == 409:
            return _find_exception(exc, RecoverableEmbeddingProviderError) is not None
        return status_code >= 500
    if _find_exception(exc, httpx.TimeoutException) is not None:
        return True
    if _find_exception(exc, httpx.NetworkError) is not None:
        return True
    if _find_exception(exc, httpx.RemoteProtocolError) is not None:
        return True
    return _find_exception(exc, RecoverableEmbeddingProviderError) is not None


def _is_non_retryable_429(exc: httpx.HTTPStatusError) -> bool:
    try:
        payload = exc.response.json()
    except ValueError:
        return False
    if not isinstance(payload, dict):
        return False
    error = payload.get("error")
    if not isinstance(error, dict):
        return False
    return any(
        isinstance(error.get(field), str)
        and error[field].strip().casefold() in NON_RETRYABLE_429_CODES
        for field in ("code", "type")
    )


def _provider_retry_delay(
    exc: Exception,
    retry_index: int,
    retry_base_seconds: float,
    random_source: RandomSource,
    jitter_max_seconds: float,
) -> float:
    retry_after = _retry_after_seconds(exc)
    if retry_after is not None:
        return retry_after
    jitter_fraction = min(1.0, max(0.0, float(random_source())))
    return (retry_base_seconds * (2**retry_index)) + (jitter_fraction * jitter_max_seconds)


def _retry_after_seconds(exc: Exception) -> float | None:
    http_error = _find_http_status_error(exc)
    if http_error is None:
        return None
    raw_value = http_error.response.headers.get("Retry-After")
    if raw_value is None:
        return None
    try:
        seconds = float(raw_value)
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, OverflowError):
            return None
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=UTC)
        seconds = (retry_at - datetime.now(UTC)).total_seconds()
    return max(0.0, seconds)


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
    except Exception:  # noqa: BLE001 - tokenizer cache 장애도 분석을 막지 않는다.
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


async def _finalize_failed_provider_request(
    ledger: AiTokenLedgerApi,
    request_id: UUID,
    usage: tuple[int, int, int] | None,
) -> None:
    """원장 정리 실패가 재시도 판단에 필요한 원래 provider 예외를 덮지 않게 한다."""

    try:
        if usage is None:
            await _complete_ledger_call_on_cancellation(
                ledger.release_ai_tokens(request_id, "USAGE_UNAVAILABLE")
            )
        else:
            await _complete_ledger_call_on_cancellation(
                ledger.settle_ai_tokens(request_id, *usage, outcome="FAILURE")
            )
    except asyncio.CancelledError:
        raise
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
