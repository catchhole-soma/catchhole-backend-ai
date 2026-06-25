from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.enums import AnalysisJobStatus, AnalysisStep


class HealthResponse(BaseModel):
    status: str = "ok"


class AnalysisJobStatusResponse(BaseModel):
    analysis_job_id: UUID
    status: AnalysisJobStatus
    progress: int = Field(ge=0, le=100)
    current_step: AnalysisStep | None
    total_count: int = 0
    processed_count: int = 0
    failed_count: int = 0
    summary: dict[str, Any] = Field(default_factory=dict)
