import asyncio
from uuid import UUID

from app.analysis.character_fact_comparator import CharacterFactComparator
from app.analysis.character_fact_comparison_pipeline import (
    CharacterFactComparisonPipeline,
    CharacterFactComparisonSpringApi,
)
from app.core.config import get_settings
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import TextGenerationClient
from app.usage.metering import MeteredTextGenerationClient


def create_character_fact_comparison_pipeline(
    spring_client: CharacterFactComparisonSpringApi,
    analysis_job_id: UUID,
    lease_token: UUID,
    comparison_model_name: str | None = None,
    provider_client: TextGenerationClient | None = None,
    request_semaphore: asyncio.Semaphore | None = None,
    max_retries: int = 3,
    retry_base_seconds: float = 2.0,
) -> CharacterFactComparisonPipeline:
    settings = get_settings()
    effective_model = comparison_model_name or settings.effective_llm_comparison_model
    provider_client = provider_client or OpenAIResponsesClient.from_settings(settings)
    comparison_client = MeteredTextGenerationClient(
        delegate=provider_client,
        ledger=spring_client,
        analysis_job_id=analysis_job_id,
        purpose="CHARACTER_FACT_COMPARISON",
        default_model=effective_model,
        lease_token=lease_token,
        request_semaphore=request_semaphore,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
    )
    return CharacterFactComparisonPipeline(
        spring_client=spring_client,
        comparator=CharacterFactComparator(
            llm_client=comparison_client,
            model=effective_model,
        ),
    )
