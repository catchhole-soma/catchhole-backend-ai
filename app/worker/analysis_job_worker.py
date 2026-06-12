from uuid import UUID

from app.domain.enums import AnalysisJobStatus, AnalysisStep
from app.schemas.analysis import AnalysisJobRunResponse


class AnalysisJobWorker:
    def run(self, analysis_job_id: UUID, force: bool = False) -> AnalysisJobRunResponse:
        # TODO: analysis_jobs 조회 후 이미 처리 중인 잡인지 확인한다.
        # TODO: episode/upload_file의 S3 key로 원문을 가져와 deterministic chunk를 생성한다.
        # TODO: 추출 후보, 근거 quote, source_chunk_id, offset을 저장하고 잡 진행률을 갱신한다.
        return AnalysisJobRunResponse(
            analysis_job_id=analysis_job_id,
            status=AnalysisJobStatus.RUNNING,
            current_step=AnalysisStep.LOADING,
            message="Analysis job accepted. Worker persistence is not wired yet.",
        )
