from functools import lru_cache

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# .env 또는 환경변수에서 설정값을 읽어오는 클래스
class Settings(BaseSettings):
    # .env 파일을 읽고 Settings에 정의되지 않은 추가 환경변수는 무시 (pydantic-settings가 .env 파일에서 이름이 같은 환경변수 이름을 자동으로 매핑)
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CatchHole AI Backend"
    app_version: str = "0.1.0"
    app_env: str = "local"

    # DB 연결 URL
    database_url: str = ""

    # AWS 설정
    aws_region: str = "ap-northeast-2"
    aws_s3_bucket: str = ""
    aws_access_key_id: str | None = None
    aws_secret_access_key: str | None = None
    aws_session_token: str | None = None
    aws_sqs_queue_url: str = ""

    # LLM API key
    llm_api_key: str = ""
    llm_model: str = "gpt-5.6-terra"
    llm_extraction_model: str | None = None
    llm_subject_resolution_model: str | None = None
    llm_comparison_model: str | None = None
    # GPT-5.6의 기본 medium 추론 비용을 자동으로 추가하지 않는 MVP 기준값
    llm_reasoning_effort: str = "none"
    openai_responses_api_url: str = "https://api.openai.com/v1/responses"
    # LLM 응답 JSON 파싱/검증 실패 시 전체 시도 횟수
    llm_extraction_max_attempts: int = 3
    # 한 프로세스 안에서 token 예약부터 provider 정산까지 동시에 수행할 LLM 요청 상한
    llm_max_concurrent_requests: int = 1
    # 429/5xx/timeout 같은 일시 provider 오류의 재시도 횟수(최초 요청 제외)
    llm_http_max_retries: int = 3
    llm_http_retry_base_seconds: float = 2.0

    # 장기 실행 Worker scheduler 설정. 로컬 기본값은 이전 단일 실행 동작을 보존한다.
    ai_worker_concurrency: int = 1
    ai_worker_idle_sleep_seconds: float = 5.0
    ai_worker_shutdown_grace_seconds: float = 180.0
    # SQLAlchemy/S3처럼 아직 동기인 I/O만 실행하는 전용 executor 크기
    ai_worker_blocking_max_workers: int = 3

    @property
    def effective_llm_extraction_model(self) -> str:
        return self.llm_extraction_model or self.llm_model

    @property
    def effective_llm_subject_resolution_model(self) -> str:
        return self.llm_subject_resolution_model or self.llm_model

    @property
    def effective_llm_comparison_model(self) -> str:
        return self.llm_comparison_model or self.llm_model

    # 청크와 검색 query가 함께 사용하는 embedding 계약
    embedding_generation_enabled: bool = False
    embedding_model: str = "text-embedding-3-small"
    # DB의 episode_chunks.embedding vector(1536)과 반드시 동일해야 함
    embedding_dimensions: int = 1536
    embedding_version: str = "v1"
    openai_embeddings_api_url: str = "https://api.openai.com/v1/embeddings"

    @field_validator("embedding_dimensions")
    @classmethod
    def validate_embedding_dimensions(cls, embedding_dimensions: int) -> int:
        if embedding_dimensions != 1536:
            raise ValueError(
                "EMBEDDING_DIMENSIONS must match episode_chunks.embedding vector(1536)."
            )
        return embedding_dimensions

    @field_validator(
        "llm_extraction_max_attempts",
        "llm_max_concurrent_requests",
        "ai_worker_concurrency",
        "ai_worker_blocking_max_workers",
    )
    @classmethod
    def validate_positive_integer(cls, value: int) -> int:
        if value < 1:
            raise ValueError("Worker concurrency and attempt settings must be at least 1.")
        return value

    @field_validator(
        "llm_http_retry_base_seconds",
        "ai_worker_idle_sleep_seconds",
        "ai_worker_shutdown_grace_seconds",
    )
    @classmethod
    def validate_positive_seconds(cls, value: float) -> float:
        if value <= 0:
            raise ValueError("Worker timing settings must be greater than zero.")
        return value

    @field_validator("llm_http_max_retries")
    @classmethod
    def validate_retry_count(cls, value: int) -> int:
        if value < 0:
            raise ValueError("LLM_HTTP_MAX_RETRIES must be zero or greater.")
        return value

    # Spring 내부 API 주소와 내부 API key를 읽음
    spring_internal_api_base_url: str = "http://localhost:8080"
    spring_internal_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
