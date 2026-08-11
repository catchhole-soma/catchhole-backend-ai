import asyncio
from uuid import UUID

from app.analysis.world_setting_comparator import (
    WorldSettingComparator,
    WorldSettingSubjectResolver,
)
from app.analysis.world_setting_pipeline import (
    WorldSettingComparisonPipeline,
    WorldSettingComparisonSpringApi,
)
from app.core.config import get_settings
from app.llm.openai_client import OpenAIResponsesClient
from app.llm.protocols import TextGenerationClient
from app.usage.metering import MeteredTextGenerationClient


def create_world_setting_comparison_pipeline(
    spring_client: WorldSettingComparisonSpringApi,
    analysis_job_id: UUID,
    lease_token: UUID,
    subject_resolution_model_name: str | None = None,
    comparison_model_name: str | None = None,
    provider_client: TextGenerationClient | None = None,
    request_semaphore: asyncio.Semaphore | None = None,
    max_retries: int = 3,
    retry_base_seconds: float = 2.0,
) -> WorldSettingComparisonPipeline:
    settings = get_settings()
    effective_subject_resolution_model = (
        subject_resolution_model_name or settings.effective_llm_subject_resolution_model
    )
    effective_comparison_model = comparison_model_name or settings.effective_llm_comparison_model
    provider_client = provider_client or OpenAIResponsesClient.from_settings(settings)
    subject_client = MeteredTextGenerationClient(
        delegate=provider_client,
        ledger=spring_client,
        analysis_job_id=analysis_job_id,
        purpose="WORLD_SETTING_SUBJECT_RESOLUTION",
        default_model=effective_subject_resolution_model,
        lease_token=lease_token,
        request_semaphore=request_semaphore,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
    )
    comparison_client = MeteredTextGenerationClient(
        delegate=provider_client,
        ledger=spring_client,
        analysis_job_id=analysis_job_id,
        purpose="WORLD_SETTING_COMPARISON",
        default_model=effective_comparison_model,
        lease_token=lease_token,
        request_semaphore=request_semaphore,
        max_retries=max_retries,
        retry_base_seconds=retry_base_seconds,
    )
    return WorldSettingComparisonPipeline(
        spring_client=spring_client,
        subject_resolver=WorldSettingSubjectResolver(
            llm_client=subject_client,
            model=effective_subject_resolution_model,
        ),
        comparator=WorldSettingComparator(
            llm_client=comparison_client,
            model=effective_comparison_model,
        ),
    )
