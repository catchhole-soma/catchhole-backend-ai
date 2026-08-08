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
from app.usage.metering import MeteredTextGenerationClient


def create_world_setting_comparison_pipeline(
    spring_client: WorldSettingComparisonSpringApi,
    analysis_job_id: UUID,
    lease_token: UUID,
    comparison_model_name: str | None = None,
) -> WorldSettingComparisonPipeline:
    settings = get_settings()
    effective_model = comparison_model_name or settings.effective_llm_comparison_model
    provider_client = OpenAIResponsesClient.from_settings(settings)
    subject_client = MeteredTextGenerationClient(
        delegate=provider_client,
        ledger=spring_client,
        analysis_job_id=analysis_job_id,
        purpose="WORLD_SETTING_SUBJECT_RESOLUTION",
        default_model=effective_model,
        lease_token=lease_token,
    )
    comparison_client = MeteredTextGenerationClient(
        delegate=provider_client,
        ledger=spring_client,
        analysis_job_id=analysis_job_id,
        purpose="WORLD_SETTING_COMPARISON",
        default_model=effective_model,
        lease_token=lease_token,
    )
    return WorldSettingComparisonPipeline(
        spring_client=spring_client,
        subject_resolver=WorldSettingSubjectResolver(
            llm_client=subject_client,
            model=effective_model,
        ),
        comparator=WorldSettingComparator(
            llm_client=comparison_client,
            model=effective_model,
        ),
    )
