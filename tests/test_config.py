import pytest
from pydantic import ValidationError

from app.core.config import Settings


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
    ],
)
def test_async_worker_settings_reject_invalid_limits(field: str, value: int) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **{field: value})
