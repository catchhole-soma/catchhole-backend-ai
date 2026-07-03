from dataclasses import dataclass
import json
from typing import Protocol
from uuid import UUID

from app.domain.enums import AnalysisStep
from app.analysis.evidence_span_resolver import resolve_candidate_evidence_offsets
from app.analysis.character_name_resolver import KnownCharacter
from app.analysis.setting_extractor import CharacterSettingExtractor
from app.clients.spring_worker_client import SpringWorkerClient
from app.db.session import get_session_maker
from app.models.episode_chunk import EpisodeChunk
from app.schemas.worker import WorkerAnalysisJobPayload
from app.services.episode_chunk_service import EpisodeChunkService
from app.services.episode_s3_chunking_service import EpisodeS3ChunkingService
from app.services.setting_candidate_service import (
    SettingCandidateSaveItem,
    SettingCandidateService,
)
from app.storage.s3 import S3TextObjectStorage

# Worker 실행 결과를 담는 값 객체
@dataclass(frozen=True)
class WorkerRunResult:
    claimed: bool
    analysis_job_id: UUID | None
    message: str

# 실제 분석 실행 후 Spring에 완료 보고할 요약 정보
@dataclass(frozen=True)
class WorkerRunSummary:
    summary_json: str | None = None
    input_token_count: int | None = None
    output_token_count: int | None = None

# SpringWorkerClient가 가져야 하는 메서드 규격
class SpringWorkerApi(Protocol):
    def claim(self, model_name: str | None = None, current_step: str | None = None) -> WorkerAnalysisJobPayload | None:
        pass

    def report_progress(self, analysis_job_id: UUID, current_step: str) -> None:
        pass

    def complete(
        self,
        analysis_job_id: UUID,
        summary_json: str | None = None,
        input_token_count: int | None = None,
        output_token_count: int | None = None,
    ) -> None:
        pass

    def fail(self, analysis_job_id: UUID, error_message: str) -> None:
        pass


# Worker가 회차 원문을 읽고 청킹 결과를 저장할 때 기대하는 규격(테스트를 위한 목적이 커서 이후에 구현 완료되면 바로 주입 가능)
class EpisodeChunkingApi(Protocol):
    def replace_chunks_from_s3_content(self, episode_id: UUID) -> list[EpisodeChunk]:
        pass


# Worker가 chunk 하나에서 설정 후보를 추출할 때 기대하는 규격(테스트를 위한 목적이 커서 이후에 구현 완료되면 바로 주입 가능)
class SettingExtractorApi(Protocol):
    def extract_from_chunk(
        self,
        source_chunk_id: UUID,
        chunk_text: str,
        episode_no: int | None = None,
        episode_title: str | None = None,
    ):
        pass


# 분석 job 하나를 claim하고, 진행/완료/실패 보고까지 수행하는 Worker
class AnalysisJobWorker:
    def __init__(
        self,
        spring_client: SpringWorkerApi | None = None,
        chunking_service: EpisodeChunkingApi | None = None,
        setting_extractor: SettingExtractorApi | None = None,
        setting_candidate_service: SettingCandidateService | None = None,
        model_name: str | None = None,
    ) -> None:
        self.spring_client = spring_client or SpringWorkerClient.from_settings()
        self._chunking_service = chunking_service
        self._setting_extractor = setting_extractor
        self._setting_candidate_service = setting_candidate_service
        self.model_name = model_name

    def run_once(self) -> WorkerRunResult:
        # Spring 서버에 처리 가능한 분석 job 하나를 요청
        payload = self.spring_client.claim(
            model_name=self.model_name,
            current_step=AnalysisStep.SETTING_EXTRACTION.value,
        )
        # 처리할 job이 없으면 아무 작업도 하지 않고 종료
        if payload is None:
            return WorkerRunResult(
                claimed=False,
                analysis_job_id=None,
                message="Claimable analysis job does not exist.",
            )

        try:
            # claim한 job의 현재 진행 상태를 Spring에 보고
            self.spring_client.report_progress(
                analysis_job_id=payload.analysis_job_id,
                current_step=AnalysisStep.SETTING_EXTRACTION.value,
            )
            # 실제 분석 로직
            summary = self._run_analysis_steps(payload)
            # 분석이 성공하면 Spring에 완료 상태와 요약 정보를 보고
            self.spring_client.complete(
                analysis_job_id=payload.analysis_job_id,
                summary_json=summary.summary_json,
                input_token_count=summary.input_token_count,
                output_token_count=summary.output_token_count,
            )
        except Exception as exc:
            self.spring_client.fail(
                analysis_job_id=payload.analysis_job_id,
                error_message=self._error_message(exc),
            )
            raise
        
        # 분석 job 하나를 정상적으로 처리했음을 반환
        return WorkerRunResult(
            claimed=True,
            analysis_job_id=payload.analysis_job_id,
            message="Analysis job completed.",
        )

    def _run_analysis_steps(self, payload: WorkerAnalysisJobPayload) -> WorkerRunSummary:
        chunk_count = 0
        save_items: list[SettingCandidateSaveItem] = []

        # Spring claim payload에 포함된 회차들을 순서대로 처리한다.
        for episode in payload.episodes:
            # 1. Episode.content_s3_key 기준으로 S3 원문을 읽고 episode_chunks를 재생성한다.
            chunks = self._get_chunking_service().replace_chunks_from_s3_content(episode.episode_id)
            chunk_count += len(chunks)

            # 2. 저장된 chunk를 LLM 추출기에 넘겨 설정 후보를 생성한다.
            for chunk in chunks:
                extraction_result = self._get_setting_extractor().extract_from_chunk(
                    source_chunk_id=chunk.id,
                    chunk_text=chunk.chunk_text,
                    episode_no=episode.episode_no,
                    episode_title=episode.title,
                )
                # 설정 후보들을 추출한 후 그 데이터를 그대로 넣고, 청크의 원문과 청크의 시작 지점을 넘겨주어 근거 위치 보정
                resolved_candidates = resolve_candidate_evidence_offsets(
                    candidates=extraction_result.candidates,
                    chunk_text=chunk.chunk_text,
                    chunk_start_offset=chunk.start_offset,
                )
                save_items.extend(
                    SettingCandidateSaveItem(
                        episode_id=episode.episode_id,
                        candidate=candidate,
                    )
                    for candidate in resolved_candidates
                )

        # 3. 검증된 후보들을 setting_candidates에 저장하고, 실제 저장된 개수를 완료 요약에 사용한다.
        saved_candidates = self._get_setting_candidate_service().replace_candidates_for_analysis_job(
            work_id=payload.work_id,
            analysis_job_id=payload.analysis_job_id,
            save_items=save_items,
            known_characters=[
                KnownCharacter(
                    character_id=character.character_id,
                    name=character.name,
                )
                for character in payload.known_characters
            ],
        )

        #Python dict를 JSON 문자열로 바꿈
        summary_json = json.dumps(
            {
                "episodeCount": len(payload.episodes),
                "chunkCount": chunk_count,
                "candidateCount": len(saved_candidates),
            },
            ensure_ascii=False,
        )
        return WorkerRunSummary(summary_json=summary_json)

    def _error_message(self, exc: Exception) -> str:
        message = str(exc) or exc.__class__.__name__
        return message[:1000]
    
    # S3에 접근해서 에피소드 원문을 청크로 나눌 EpisodeS3ChunkingService를 초기화 하는 작업만 한다.
    def _get_chunking_service(self) -> EpisodeChunkingApi:
        if self._chunking_service is None:
            session_factory = get_session_maker()
            self._chunking_service = EpisodeS3ChunkingService(
                session_factory=session_factory,
                storage=S3TextObjectStorage.from_settings(),
                chunk_service=EpisodeChunkService(session_factory=session_factory),
            )
        return self._chunking_service

    # llm에 넣을 프롬프트와 api호출을 할 서비스(CharacterSettingExtractor)를 초기화 하는 작업만 한다.
    def _get_setting_extractor(self) -> SettingExtractorApi:
        if self._setting_extractor is None:
            self._setting_extractor = CharacterSettingExtractor(model=self.model_name)
        return self._setting_extractor

    # 검증된 설정 후보를 setting_candidates 테이블에 저장할 서비스를 필요할 때 초기화한다.
    def _get_setting_candidate_service(self) -> SettingCandidateService:
        if self._setting_candidate_service is None:
            self._setting_candidate_service = SettingCandidateService(
                session_factory=get_session_maker(),
            )
        return self._setting_candidate_service
