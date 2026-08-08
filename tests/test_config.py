from app.core.config import Settings


def test_stage_models_fall_back_to_legacy_model() -> None:
    settings = Settings(
        _env_file=None,
        llm_model="legacy-model",
        llm_extraction_model=None,
        llm_comparison_model=None,
    )

    assert settings.effective_llm_extraction_model == "legacy-model"
    assert settings.effective_llm_comparison_model == "legacy-model"


def test_stage_models_can_be_configured_independently() -> None:
    settings = Settings(
        _env_file=None,
        llm_model="legacy-model",
        llm_extraction_model="extraction-model",
        llm_comparison_model="comparison-model",
    )

    assert settings.effective_llm_extraction_model == "extraction-model"
    assert settings.effective_llm_comparison_model == "comparison-model"


def test_stage_models_are_loaded_from_independent_environment_variables(monkeypatch) -> None:
    monkeypatch.setenv("LLM_EXTRACTION_MODEL", "env-extraction-model")
    monkeypatch.setenv("LLM_COMPARISON_MODEL", "env-comparison-model")

    settings = Settings(_env_file=None)

    assert settings.effective_llm_extraction_model == "env-extraction-model"
    assert settings.effective_llm_comparison_model == "env-comparison-model"
