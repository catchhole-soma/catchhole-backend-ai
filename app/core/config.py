from functools import lru_cache

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

    #Spring 내부 API 주소와 내부 API key를 읽음
    spring_internal_api_base_url: str = "http://localhost:8080"
    spring_internal_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
