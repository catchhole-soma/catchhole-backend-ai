import pytest
from pydantic import ValidationError

from app.core.config import Settings


def test_purpose_output_caps_use_operational_defaults() -> None:
    defaults = Settings.model_fields

    assert defaults["llm_setting_extraction_max_output_tokens"].default == 6000
    assert defaults["llm_setting_extraction_retry_max_output_tokens"].default == 12000
    assert defaults["llm_world_setting_extraction_max_output_tokens"].default == 5000
    assert defaults["llm_world_setting_extraction_retry_max_output_tokens"].default == 10000
    assert defaults["llm_subject_resolution_max_output_tokens"].default == 2000
    assert defaults["llm_comparison_max_output_tokens"].default == 3000
    assert defaults["llm_world_setting_batch_comparison_max_output_tokens"].default == 16000
    assert defaults["llm_provider_max_output_tokens"].default == 128000


def test_stage_models_fall_back_to_legacy_model() -> None:
    settings = Settings(
        _env_file=None,
        llm_model="legacy-model",
        llm_extraction_model=None,
        llm_subject_resolution_model=None,
        llm_comparison_model=None,
    )

    assert settings.effective_llm_extraction_model == "legacy-model"
    assert settings.effective_llm_subject_resolution_model == "legacy-model"
    assert settings.effective_llm_comparison_model == "legacy-model"


def test_stage_models_can_be_configured_independently() -> None:
    settings = Settings(
        _env_file=None,
        llm_model="legacy-model",
        llm_extraction_model="extraction-model",
        llm_subject_resolution_model="subject-resolution-model",
        llm_comparison_model="comparison-model",
    )

    assert settings.effective_llm_extraction_model == "extraction-model"
    assert settings.effective_llm_subject_resolution_model == "subject-resolution-model"
    assert settings.effective_llm_comparison_model == "comparison-model"


def test_stage_models_are_loaded_from_independent_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("LLM_EXTRACTION_MODEL", "env-extraction-model")
    monkeypatch.setenv(
        "LLM_SUBJECT_RESOLUTION_MODEL",
        "env-subject-resolution-model",
    )
    monkeypatch.setenv("LLM_COMPARISON_MODEL", "env-comparison-model")

    settings = Settings(_env_file=None)

    assert settings.effective_llm_extraction_model == "env-extraction-model"
    assert settings.effective_llm_subject_resolution_model == "env-subject-resolution-model"
    assert settings.effective_llm_comparison_model == "env-comparison-model"


def test_async_worker_settings_are_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("AI_WORKER_CONCURRENCY", "5")
    monkeypatch.setenv("LLM_MAX_CONCURRENT_REQUESTS", "5")
    monkeypatch.setenv("AI_WORKER_IDLE_SLEEP_SECONDS", "2.5")
    monkeypatch.setenv("AI_WORKER_SHUTDOWN_GRACE_SECONDS", "180")
    monkeypatch.setenv("AI_WORKER_BLOCKING_MAX_WORKERS", "2")
    monkeypatch.setenv("LLM_HTTP_MAX_RETRIES", "3")
    monkeypatch.setenv("LLM_HTTP_RETRY_BASE_SECONDS", "2")

    settings = Settings(_env_file=None)

    assert settings.ai_worker_concurrency == 5
    assert settings.llm_max_concurrent_requests == 5
    assert settings.ai_worker_idle_sleep_seconds == 2.5
    assert settings.ai_worker_shutdown_grace_seconds == 180
    assert settings.ai_worker_blocking_max_workers == 2
    assert settings.llm_http_max_retries == 3
    assert settings.llm_http_retry_base_seconds == 2


def test_database_pool_settings_are_loaded_from_environment(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_POOL_SIZE", "4")
    monkeypatch.setenv("DATABASE_POOL_MAX_OVERFLOW", "1")
    monkeypatch.setenv("TZ", "Asia/Tokyo")

    settings = Settings(_env_file=None)

    assert settings.database_pool_size == 4
    assert settings.database_pool_max_overflow == 1
    assert settings.tz == "Asia/Tokyo"


def test_purpose_output_caps_are_loaded_independently(monkeypatch) -> None:
    monkeypatch.setenv("LLM_SETTING_EXTRACTION_MAX_OUTPUT_TOKENS", "4100")
    monkeypatch.setenv("LLM_SETTING_EXTRACTION_RETRY_MAX_OUTPUT_TOKENS", "8200")
    monkeypatch.setenv("LLM_WORLD_SETTING_EXTRACTION_MAX_OUTPUT_TOKENS", "3100")
    monkeypatch.setenv("LLM_WORLD_SETTING_EXTRACTION_RETRY_MAX_OUTPUT_TOKENS", "6200")
    monkeypatch.setenv("LLM_SUBJECT_RESOLUTION_MAX_OUTPUT_TOKENS", "1100")
    monkeypatch.setenv("LLM_COMPARISON_MAX_OUTPUT_TOKENS", "2100")
    monkeypatch.setenv("LLM_WORLD_SETTING_BATCH_COMPARISON_MAX_OUTPUT_TOKENS", "9100")
    monkeypatch.setenv("LLM_PROVIDER_MAX_OUTPUT_TOKENS", "128000")

    settings = Settings(_env_file=None)

    assert settings.llm_setting_extraction_max_output_tokens == 4100
    assert settings.llm_setting_extraction_retry_max_output_tokens == 8200
    assert settings.llm_world_setting_extraction_max_output_tokens == 3100
    assert settings.llm_world_setting_extraction_retry_max_output_tokens == 6200
    assert settings.llm_subject_resolution_max_output_tokens == 1100
    assert settings.llm_comparison_max_output_tokens == 2100
    assert settings.llm_world_setting_batch_comparison_max_output_tokens == 9100
    assert settings.llm_provider_max_output_tokens == 128000


@pytest.mark.parametrize(
    "overrides",
    [
        {
            "llm_setting_extraction_max_output_tokens": 8001,
            "llm_setting_extraction_retry_max_output_tokens": 8000,
        },
        {
            "llm_comparison_max_output_tokens": 2001,
            "llm_provider_max_output_tokens": 2000,
        },
        {
            "llm_world_setting_batch_comparison_max_output_tokens": 2001,
            "llm_provider_max_output_tokens": 2000,
        },
        {
            "llm_world_setting_extraction_max_output_tokens": 10001,
            "llm_world_setting_extraction_retry_max_output_tokens": 10000,
        },
        {"llm_setting_extraction_max_output_tokens": 0},
    ],
)
def test_purpose_output_caps_reject_invalid_configuration(overrides: dict) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("ai_worker_concurrency", 0),
        ("llm_max_concurrent_requests", 0),
        ("ai_worker_blocking_max_workers", 0),
        ("ai_worker_idle_sleep_seconds", 0),
        ("ai_worker_shutdown_grace_seconds", 0),
        ("llm_http_retry_base_seconds", 0),
        ("llm_http_max_retries", -1),
        ("database_pool_size", 0),
        ("database_pool_max_overflow", -1),
    ],
)
def test_async_worker_settings_reject_invalid_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
