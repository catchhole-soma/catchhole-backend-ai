from functools import lru_cache
from typing import Literal

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
    aws_sqs_queue_url: str = ""

    # LLM API key
    llm_api_key: str = ""
    llm_model: str = "gpt-4.1-mini"
    openai_responses_api_url: str = "https://api.openai.com/v1/responses"
    # LLM 응답 JSON 파싱/검증 실패 시 전체 시도 횟수
    llm_extraction_max_attempts: int = 3

    # 청크와 검색 query가 함께 사용하는 embedding 계약
    embedding_model: str = "text-embedding-3-small"
    # DB의 episode_chunks.embedding vector(1536)과 반드시 동일해야 함
    embedding_dimensions: Literal[1536] = 1536
    embedding_version: str = "v1"
    openai_embeddings_api_url: str = "https://api.openai.com/v1/embeddings"

    #Spring 내부 API 주소와 내부 API key를 읽음
    spring_internal_api_base_url: str = "http://localhost:8080"
    spring_internal_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
