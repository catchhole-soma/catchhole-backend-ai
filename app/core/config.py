from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "CatchHole AI Backend"
    app_version: str = "0.1.0"
    app_env: str = "local"

    database_url: str = ""

    aws_region: str = "ap-northeast-2"
    aws_s3_bucket: str = ""
    aws_sqs_queue_url: str = ""

    llm_api_key: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
