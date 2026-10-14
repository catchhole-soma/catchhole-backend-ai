from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.analysis.semantic_current_context import SemanticCurrentContext
from app.domain.enums import (
    AnalysisFailureCode,
    AnalysisJobCheckpointStage,
    AnalysisJobType,
    AnalysisMode,
    AnalysisReviewMode,
    AnalysisSourceKind,
    CharacterFactComparisonOperation,
    CharacterFactComparisonStatus,
    CharacterFactTemporalScope,
    EpisodeProcessingStatus,
    SettingBookReferenceTimeKind,
    SettingValueType,
    WorldSettingCategory,
    WorldSettingComparisonReviewReason,
    WorldSettingComparisonValidationReason,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
    WorldSettingSubjectResolutionType,
)
from app.schemas.analysis_context import AnalysisStateProvenance, WorkerAnalysisContext
from app.schemas.analysis_source import SettingBookReferenceTime, WorkerAnalysisSource

# Spring AI 토큰 원장 계약에서 허용하는 호출 목적과 종료 결과
AiTokenPurpose = Literal[
    "SETTING_EXTRACTION",
    "SUBJECT_RESOLUTION",
    "CHARACTER_FACT_COMPARISON",
    "CHUNK_EMBEDDING",
    "WORLD_SETTING_EXTRACTION",
    "WORLD_SETTING_SUBJECT_RESOLUTION",
    "WORLD_SETTING_COMPARISON",
]
AiTokenUsageOutcome = Literal[
    "SUCCESS",
    "FAILURE",
    "USAGE_UNAVAILABLE",
    "WORKER_LEASE_EXPIRED",
]


def _validate_status_active_value(
    fact_type: str,
    value_json: Any | None,
    field_name: str,
) -> None:
    if fact_type != "STATUS" or not isinstance(value_json, dict) or "active" not in value_json:
        return
    if type(value_json["active"]) is not bool:
        raise ValueError(f"{field_name}.active must be a JSON boolean.")


# Worker가 Spring 서버에 job claim 요청
class WorkerAnalysisJobClaimRequest(BaseModel):
    # Python 필드명과 JSON alias를 둘 다 허용한다, 예: model_name or modelName 모두 가능
    # Pydantic 모델의 설정값, 실제 데이터 필드로 들어가지 않음
    model_config = ConfigDict(populate_by_name=True)

    model_name: str | None = Field(default=None, alias="modelName", max_length=100)
    # Spring에 알려줄 현재 작업 단계
    current_step: str | None = Field(default=None, alias="currentStep", max_length=100)
    allowed_job_types: list[AnalysisJobType] = Field(
        alias="allowedJobTypes",
        min_length=1,
    )
    supported_analysis_modes: list[AnalysisMode] | None = Field(
        default=None, alias="supportedAnalysisModes", min_length=1
    )
    supports_character_comparison_groups: bool | None = Field(
        default=None,
        alias="supportsCharacterComparisonGroups",
    )
    supported_source_kinds: list[AnalysisSourceKind] | None = Field(default=None, alias="supportedSourceKinds")
    supports_setting_book_reference_time: bool | None = Field(
        default=None, alias="supportsSettingBookReferenceTime", strict=True,
    )
    supports_semantic_current_context: bool | None = Field(
        default=None, alias="supportsSemanticCurrentContext", strict=True,
    )


# Worker가 분석 진행 상황을 Spring에 보고할 때 쓰는 DTO
class WorkerAnalysisJobProgressRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    # 현재 진행 단계, 빈 문자열은 허용 x
    current_step: str = Field(alias="currentStep", min_length=1, max_length=100)
    # 사람이 읽는 currentStep과 별개로 Spring Episode에 적용할 명시적 상태
    episode_status: EpisodeProcessingStatus | None = Field(default=None, alias="episodeStatus")
    checkpoint_stage: AnalysisJobCheckpointStage | None = Field(
        default=None,
        alias="checkpointStage",
    )


# Worker가 분석 성공을 Spring에 보고할 때 쓰는 DTO
class WorkerAnalysisJobCompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # 분석 결과 요약 JSON 문자열
    summary_json: str | None = Field(default=None, alias="summaryJson")
    # 구버전 Worker 호환용 필드. Backend는 token ledger 합계를 사용한다.
    input_token_count: int | None = Field(default=None, alias="inputTokenCount", ge=0)
    # 구버전 Worker 호환용 필드. Backend는 token ledger 합계를 사용한다.
    output_token_count: int | None = Field(default=None, alias="outputTokenCount", ge=0)


# Worker가 분석 실패를 Spring에 보고할 때 쓰는 DTO
class WorkerAnalysisJobFailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    failure_code: AnalysisFailureCode = Field(alias="failureCode")
    # 실패 사유
    error_message: str = Field(alias="errorMessage", min_length=1)


class WorkerAnalysisJobHeartbeatResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    lease_token: UUID = Field(alias="leaseToken")
    lease_expires_at: datetime = Field(alias="leaseExpiresAt")


# Provider 호출 직전에 예상 최대 토큰을 Spring 원장에 예약할 때 쓰는 DTO
class AiTokenReserveRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # 재시도 통신에서도 같은 예약을 식별해 중복 차감을 막는 요청 ID
    request_id: UUID = Field(alias="requestId")
    analysis_job_id: UUID = Field(alias="analysisJobId")
    purpose: AiTokenPurpose
    # 같은 분석 작업과 목적 안에서 발생한 provider 호출 순번
    attempt: int = Field(ge=1)
    model_name: str = Field(alias="modelName", min_length=1, max_length=100)
    reserved_tokens: int = Field(alias="reservedTokens", ge=1)


# Provider가 usage를 반환한 요청의 실제 입력·출력 토큰을 정산할 때 쓰는 DTO
class AiTokenSettleRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    input_tokens: int = Field(alias="inputTokens", ge=0)
    # cached input은 input_tokens에 이미 포함되므로 관측값으로만 별도 전달한다.
    cached_input_tokens: int = Field(alias="cachedInputTokens", ge=0)
    output_tokens: int = Field(alias="outputTokens", ge=0)
    outcome: AiTokenUsageOutcome


# 실제 usage를 알 수 없는 요청의 예약량을 전액 해제할 때 쓰는 DTO
class AiTokenReleaseRequest(BaseModel):
    outcome: AiTokenUsageOutcome


# Spring이 Worker에게 내려주는 회차 정보 DTO
class WorkerAnalysisEpisodePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    episode_id: UUID = Field(alias="episodeId")
    episode_no: int = Field(alias="episodeNo")
    title: str | None = None
    content_s3_key: str = Field(alias="contentS3Key")
    content_s3_version: str | None = Field(default=None, alias="contentS3Version")
    content_hash: str | None = Field(default=None, alias="contentHash")
    char_count: int = Field(alias="charCount")


class WorkerAnalysisSettingBookPayload(SettingBookReferenceTime):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    setting_book_id: UUID = Field(alias="settingBookId")
    original_filename: str = Field(alias="originalFilename", min_length=1)
    revision_id: UUID = Field(alias="revisionId")
    revision_no: int = Field(alias="revisionNo", ge=1)
    content_s3_key: str = Field(alias="contentS3Key", min_length=1)
    content_s3_version: str | None = Field(default=None, alias="contentS3Version")
    content_hash: str = Field(alias="contentHash", pattern=r"^[0-9a-f]{64}$")
    char_count: int = Field(alias="charCount", ge=0)

    def prompt_data(self) -> dict:
        return {"source_kind": "SETTING_BOOK", "setting_book_name": self.original_filename,
                "revision_no": self.revision_no, **self.reference_prompt_data()}


# Spring이 Worker에게 내려주는 캐릭터별 활성 STATUS 최소 문맥 DTO
class WorkerEvidenceSpan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    quote: str = Field(min_length=1)
    start_offset: int | None = Field(default=None, alias="startOffset", ge=0)
    end_offset: int | None = Field(default=None, alias="endOffset", ge=0)


class WorkerAnalysisActiveCharacterStatusPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    fact_key: str = Field(alias="factKey", min_length=1, max_length=150)
    # provenance가 없는 legacy snapshot은 표시값이 없을 수 있으며 Worker가 값을 합성하지 않는다.
    fact_value: str | None = Field(alias="factValue")
    provenance: AnalysisStateProvenance | None = None


# Spring이 Worker에게 내려주는 기존 캐릭터 정보 DTO
class WorkerProtectedCharacterSlot(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    fact_type: str = Field(alias="factType", min_length=1, max_length=30)
    fact_key: str = Field(alias="factKey", min_length=1, max_length=150)

    def prompt_data(self):
        return self.model_dump(mode="json")


class WorkerAnalysisKnownCharacterPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    character_id: UUID = Field(alias="characterId")
    name: str
    provenance: AnalysisStateProvenance | None = None
    aliases: list[str] = Field(default_factory=list)
    identity_evidence: list[WorkerEvidenceSpan] = Field(default_factory=list, alias="identityEvidence")
    active_statuses: list[WorkerAnalysisActiveCharacterStatusPayload] = Field(
        default_factory=list,
        alias="activeStatuses",
    )
    protected_slots: list[WorkerProtectedCharacterSlot] = Field(default_factory=list, alias="protectedSlots")


class WorkerAnalysisProvisionalCharacterPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    provisional_subject_key: str = Field(
        alias="provisionalSubjectKey", pattern=r"^provisional-character:[0-9a-f-]{36}$"
    )
    name: str = Field(min_length=1, max_length=100)
    aliases: list[str] = Field(default_factory=list)
    source_episode_no: int | None = Field(default=None, alias="sourceEpisodeNo", ge=1)
    source: WorkerAnalysisSource | None = None
    identity_evidence: list[WorkerEvidenceSpan] = Field(
        default_factory=list, alias="identityEvidence"
    )
    active_statuses: list[WorkerAnalysisActiveCharacterStatusPayload] = Field(
        default_factory=list, alias="activeStatuses"
    )

    @model_validator(mode="after")
    def validate_source(self):
        if self.source is not None and self.source.source_kind == AnalysisSourceKind.SETTING_BOOK:
            if self.source_episode_no is not None:
                raise ValueError("Book provisional characters cannot have an episode number.")
        elif self.source_episode_no is None:
            raise ValueError("Episode provisional characters require their source episode number.")
        return self


# Spring이 Worker에게 내려주는 캐릭터 설정 schema hint DTO
class WorkerAnalysisCharacterSettingSchemaPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    schema_key: str = Field(alias="schemaKey")
    display_name: str = Field(alias="displayName")
    attribute_pattern: str | None = Field(default=None, alias="attributePattern")
    aliases: list[str] = Field(default_factory=list)
    value_type: Literal["STRING", "NUMBER", "BOOLEAN", "JSON", "UNKNOWN"] = Field(alias="valueType")


# Spring이 Worker에게 내려주는 분석 job 전체 payload
class WorkerAnalysisJobPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    analysis_job_id: UUID = Field(alias="analysisJobId")
    job_type: AnalysisJobType = Field(alias="jobType")
    work_id: UUID = Field(alias="workId")
    work_title: str = Field(alias="workTitle")
    # 사용자 재비교용 hidden Job은 레거시 후보에 원본 batch/episode가 없을 수 있다.
    # 일반 회차 분석 Worker는 처리 시작 시 두 값을 별도로 필수 검증한다.
    batch_id: UUID | None = Field(default=None, alias="batchId")
    model_name: str | None = Field(default=None, alias="modelName")
    current_step: str | None = Field(default=None, alias="currentStep")
    lease_token: UUID = Field(alias="leaseToken")
    lease_expires_at: datetime = Field(alias="leaseExpiresAt")
    claim_attempt_count: int = Field(alias="claimAttemptCount", ge=1)
    checkpoint_stage: AnalysisJobCheckpointStage | None = Field(
        default=None,
        alias="checkpointStage",
    )
    world_setting_candidate_id: UUID | None = Field(
        default=None,
        alias="worldSettingCandidateId",
    )
    setting_candidate_id: UUID | None = Field(
        default=None,
        alias="settingCandidateId",
    )
    character_setting_schemas: list[WorkerAnalysisCharacterSettingSchemaPayload] = Field(
        default_factory=list,
        alias="characterSettingSchemas",
    )
    known_characters: list[WorkerAnalysisKnownCharacterPayload] = Field(
        default_factory=list,
        alias="knownCharacters",
    )
    episode: WorkerAnalysisEpisodePayload | None = None
    source_kind: AnalysisSourceKind = Field(default=AnalysisSourceKind.EPISODE, alias="sourceKind")
    setting_book: WorkerAnalysisSettingBookPayload | None = Field(default=None, alias="settingBook")
    analysis_mode: AnalysisMode = Field(
        default=AnalysisMode.CONFIRMED_ONLY, alias="analysisMode"
    )
    review_mode: AnalysisReviewMode = Field(default=AnalysisReviewMode.MANUAL, alias="reviewMode")
    analysis_context: WorkerAnalysisContext | None = Field(default=None, alias="analysisContext")
    semantic_current_context: SemanticCurrentContext | None = Field(
        default=None, alias="semanticCurrentContext", exclude_if=lambda value: value is None,
    )
    provisional_characters: list[WorkerAnalysisProvisionalCharacterPayload] = Field(
        default_factory=list, alias="provisionalCharacters"
    )

    def frozen_source(self) -> WorkerAnalysisSource:
        if self.source_kind == AnalysisSourceKind.SETTING_BOOK:
            book = self.setting_book
            return WorkerAnalysisSource(source_kind=self.source_kind, setting_book_id=book.setting_book_id,
                setting_book_name=book.original_filename, revision_id=book.revision_id,
                revision_no=book.revision_no, content_hash=book.content_hash,
                reference_time_kind=book.reference_time_kind, reference_episode_no=book.reference_episode_no)
        return WorkerAnalysisSource(source_kind=self.source_kind,
            episode_id=self.episode.episode_id if self.episode else None,
            episode_no=self.episode.episode_no if self.episode else None,
            content_hash=self.episode.content_hash if self.episode else None)

    @field_validator("review_mode", mode="before")
    @classmethod
    def default_review_mode(cls, value):
        return AnalysisReviewMode.MANUAL if value is None else value

    @model_validator(mode="after")
    def validate_analysis_mode(self) -> "WorkerAnalysisJobPayload":
        is_book = self.source_kind == AnalysisSourceKind.SETTING_BOOK
        if is_book:
            if self.episode is not None or self.setting_book is None:
                raise ValueError("Setting-book jobs require a book snapshot and no episode.")
            if self.analysis_context is not None and (
                (self.analysis_context.reference_time_kind or SettingBookReferenceTimeKind.UNSPECIFIED)
                    != self.setting_book.reference_time_kind
                or self.analysis_context.reference_episode_no != self.setting_book.reference_episode_no
            ):
                raise ValueError("Setting-book reference time does not match its frozen input context.")
            if self.job_type == AnalysisJobType.SETTING_EXTRACTION and (
                self.analysis_mode != AnalysisMode.ORDERED_PROVISIONAL
                or self.review_mode != AnalysisReviewMode.AUTOMATIC
            ):
                raise ValueError("Setting-book extraction requires automatic ordered analysis.")
        elif self.setting_book is not None:
            raise ValueError("Episode jobs cannot include a setting-book snapshot.")
        if self.analysis_mode == AnalysisMode.CONFIRMED_ONLY:
            if self.review_mode == AnalysisReviewMode.AUTOMATIC:
                raise ValueError("Automatic review requires ordered episode analysis.")
            if self.analysis_context is not None or self.provisional_characters:
                raise ValueError("Confirmed-only jobs cannot include ordered analysis state.")
            if any(status.provenance is not None
                   and status.provenance.confirmation_status == "PROVISIONAL"
                   for character in self.known_characters for status in character.active_statuses):
                raise ValueError("Confirmed-only jobs cannot include provisional active statuses.")
        else:
            if self.analysis_context is None or (not is_book and self.episode is None):
                raise ValueError("Ordered analysis requires a fixed input context and source.")
            if self.job_type != AnalysisJobType.SETTING_EXTRACTION:
                raise ValueError("Only episode extraction jobs support ordered analysis.")
            source = self.setting_book if is_book else self.episode
            if source.content_hash != self.analysis_context.source_hash:
                raise ValueError("Ordered source hash must match the source snapshot.")
            if not is_book and any(
                item.source_episode_no is not None and item.source_episode_no >= self.episode.episode_no
                for item in self.provisional_characters
                if item.source is None or item.source.source_kind == AnalysisSourceKind.EPISODE
            ):
                raise ValueError("Provisional character context must precede this episode.")
            if not is_book and any(
                item.source_episode_no is not None and item.source_episode_no >= self.episode.episode_no
                for item in self.analysis_context.unresolved_references
                if item.source is None or item.source.source_kind == AnalysisSourceKind.EPISODE
            ):
                raise ValueError("Unresolved reference context must precede this episode.")
        return self


class WorkerWorldSettingCandidatePublishItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    category: WorldSettingCategory
    subject_name: str = Field(alias="subjectName", min_length=1, max_length=100)
    scope_name: str | None = Field(default=None, alias="scopeName", max_length=100)
    setting_name: str = Field(alias="settingName", min_length=1, max_length=100)
    extracted_value: str = Field(alias="extractedValue", min_length=1)
    evidence_spans: list[WorkerEvidenceSpan] = Field(alias="evidenceSpans", min_length=1)
    extraction_confidence: float = Field(
        alias="extractionConfidence", strict=True, ge=0, le=1, allow_inf_nan=False,
    )
    raw_extraction_json: dict[str, Any] | None = Field(
        default=None,
        alias="rawExtractionJson",
    )
    temporal_scope: CharacterFactTemporalScope | None = Field(
        default=None, alias="temporalScope", exclude_if=lambda value: value is None,
    )
    effective_from_episode_no: int | None = Field(
        default=None, alias="effectiveFromEpisodeNo", ge=1, strict=True, exclude_if=lambda value: value is None,
    )


class WorkerWorldSettingCandidatePublishRequest(BaseModel):
    candidates: list[WorkerWorldSettingCandidatePublishItem]


class WorkerWorldSourcePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    source_episode_id: UUID | None = Field(default=None, alias="sourceEpisodeId")
    source: WorkerAnalysisSource | None = None

    @model_validator(mode="after")
    def validate_source(self):
        if self.source is not None and self.source.source_kind == AnalysisSourceKind.SETTING_BOOK:
            if self.source_episode_id is not None:
                raise ValueError("Book world candidates cannot have a source episode.")
        elif self.source_episode_id is None:
            raise ValueError("World candidates require a known source.")
        elif self.source is not None and self.source.episode_id != self.source_episode_id:
            raise ValueError("World candidate source episode does not match its provenance.")
        return self


class WorkerWorldSettingCandidatePayload(WorkerWorldSourcePayload):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    work_id: UUID = Field(alias="workId")
    category: WorldSettingCategory
    subject_name: str = Field(alias="subjectName")
    scope_name: str | None = Field(default=None, alias="scopeName")
    setting_name: str = Field(alias="settingName")
    extracted_value: str = Field(alias="extractedValue")
    evidence_spans: list[WorkerEvidenceSpan] = Field(alias="evidenceSpans")
    extraction_confidence: float | None = Field(default=None, alias="extractionConfidence")
    temporal_scope: CharacterFactTemporalScope | None = Field(default=None, alias="temporalScope")
    effective_from_episode_no: int | None = Field(default=None, alias="effectiveFromEpisodeNo", ge=1, strict=True)
    raw_extraction_json: Any = Field(default=None, alias="rawExtractionJson", exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def default_temporal_scope(self):
        if self.temporal_scope is None:
            self.temporal_scope = (CharacterFactTemporalScope.UNKNOWN
                if self.source is not None and self.source.source_kind == AnalysisSourceKind.SETTING_BOOK
                else CharacterFactTemporalScope.PRESENT)
        return self


class WorkerWorldSettingComparisonBatchCandidate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_ref: str = Field(alias="candidateRef", pattern=r"^C[1-9][0-9]*$")
    candidate_id: UUID = Field(alias="candidateId")
    subject_name: str = Field(alias="subjectName", min_length=1, max_length=100)
    scope_name: str | None = Field(default=None, alias="scopeName", max_length=100)
    setting_name: str = Field(alias="settingName", min_length=1, max_length=100)
    extracted_value: str = Field(alias="extractedValue", min_length=1)
    evidence_spans: list[WorkerEvidenceSpan] = Field(alias="evidenceSpans", min_length=1)
    extraction_confidence: float | None = Field(default=None, alias="extractionConfidence")
    temporal_scope: CharacterFactTemporalScope | None = Field(default=None, alias="temporalScope")
    effective_from_episode_no: int | None = Field(default=None, alias="effectiveFromEpisodeNo", ge=1, strict=True)
    raw_extraction_json: Any = Field(default=None, alias="rawExtractionJson", exclude_if=lambda value: value is None)


class WorkerWorldSettingSubjectResolutionCandidate(WorkerWorldSourcePayload):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    category: WorldSettingCategory
    subject_name: str = Field(alias="subjectName", min_length=1, max_length=100)
    evidence_spans: list[WorkerEvidenceSpan] = Field(default_factory=list, alias="evidenceSpans")
    source_episode_no: int | None = Field(default=None, alias="sourceEpisodeNo", ge=1)
    raw_extraction_json: Any = Field(default=None, alias="rawExtractionJson", exclude_if=lambda value: value is None)


class WorkerWorldSettingSubjectResolutionPendingResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    candidates: list[WorkerWorldSettingSubjectResolutionCandidate]
    analysis_context: WorkerAnalysisContext | None = Field(default=None, alias="analysisContext")


class WorkerWorldSettingSubjectResolutionRequestItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    target_world_setting_ids: list[UUID] = Field(
        alias="targetWorldSettingIds",
        max_length=20,
    )
    provisional_subject_keys: list[str] = Field(default_factory=list, alias="provisionalSubjectKeys", max_length=20)
    ambiguous: bool = False
    failure_code: AnalysisFailureCode | None = Field(default=None, alias="failureCode", exclude_if=lambda value: value is None)

    @model_validator(mode="after")
    def validate_selected_identities(self):
        keys = self.provisional_subject_keys
        if self.failure_code is not None:
            if self.failure_code not in {
                AnalysisFailureCode.LLM_NETWORK_ERROR, AnalysisFailureCode.LLM_PROVIDER_ERROR,
                AnalysisFailureCode.LLM_OUTPUT_TRUNCATED, AnalysisFailureCode.LLM_RESPONSE_PARSE_ERROR,
                AnalysisFailureCode.COMPARISON_VALIDATION_FAILED,
            }:
                raise ValueError("World subject failures must be candidate-local response failures.")
            if self.ambiguous or keys or self.target_world_setting_ids:
                raise ValueError("Failed world subject resolution cannot select or establish an identity.")
        if len(self.target_world_setting_ids) + len(keys) > 20:
            raise ValueError("World subject selection exceeds the combined target limit.")
        if len(keys) != len(set(keys)):
            raise ValueError("Provisional world subject keys must be unique.")
        if any(not key.startswith("provisional-world:") for key in keys):
            raise ValueError("Provisional world targets require explicit provisional keys.")
        if self.ambiguous and (keys or self.target_world_setting_ids):
            raise ValueError("Ambiguous world subjects cannot select a target.")
        return self


class WorkerWorldSettingSubjectResolutionRequest(BaseModel):
    resolutions: list[WorkerWorldSettingSubjectResolutionRequestItem] = Field(min_length=1)


class WorkerWorldSettingSubjectResolutionResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    resolution_type: WorldSettingSubjectResolutionType = Field(alias="resolutionType")
    canonical_subject_key: str = Field(alias="canonicalSubjectKey", min_length=1)
    canonical_subject_name: str = Field(alias="canonicalSubjectName", min_length=1)
    target_world_setting_ids: list[UUID] = Field(alias="targetWorldSettingIds", max_length=20)
    provisional_subject_keys: list[str] = Field(default_factory=list, alias="provisionalSubjectKeys", max_length=20)


class WorkerWorldSettingSubjectResolutionResponse(BaseModel):
    resolutions: list[WorkerWorldSettingSubjectResolutionResult]


class WorkerWorldSettingComparisonBatchPayload(WorkerWorldSourcePayload):
    model_config = ConfigDict(populate_by_name=True)

    comparison_batch_id: UUID = Field(alias="comparisonBatchId")
    work_id: UUID = Field(alias="workId")
    category: WorldSettingCategory
    resolution_type: WorldSettingSubjectResolutionType = Field(alias="resolutionType")
    canonical_subject_key: str = Field(alias="canonicalSubjectKey", min_length=1)
    canonical_subject_name: str = Field(
        alias="canonicalSubjectName",
        min_length=1,
        max_length=100,
    )
    resolved_target_world_setting_ids: list[UUID] = Field(
        alias="resolvedTargetWorldSettingIds",
        max_length=20,
    )
    resolved_provisional_subject_keys: list[str] = Field(default_factory=list, alias="resolvedProvisionalSubjectKeys", max_length=20)
    analysis_context: WorkerAnalysisContext | None = Field(default=None, alias="analysisContext")
    raw_scope_name: str | None = Field(default=None, alias="rawScopeName")
    candidates: list[WorkerWorldSettingComparisonBatchCandidate] = Field(
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def default_candidate_temporal_scope(self):
        for candidate in self.candidates:
            if candidate.temporal_scope is None:
                candidate.temporal_scope = (CharacterFactTemporalScope.UNKNOWN
                    if self.source is not None and self.source.source_kind == AnalysisSourceKind.SETTING_BOOK
                    else CharacterFactTemporalScope.PRESENT)
        return self


class WorkerWorldSubjectIdentity(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    world_setting_id: UUID | None = Field(default=None, alias="worldSettingId")
    provisional_subject_key: str | None = Field(default=None, alias="provisionalSubjectKey", pattern=r"^provisional-world:[0-9a-f-]{36}$")

    @model_validator(mode="after")
    def validate_identity(self) -> "WorkerWorldSubjectIdentity":
        if (self.world_setting_id is None) == (self.provisional_subject_key is None):
            raise ValueError("Exactly one actual or provisional world identity is required.")
        return self

    def identity_key(self) -> str:
        return self.provisional_subject_key or f"world:{self.world_setting_id}"


class WorkerProtectedWorldPath(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    scope_name: str | None = Field(default=None, alias="scopeName", max_length=100)
    setting_name: str = Field(alias="settingName", min_length=1, max_length=100)

    def prompt_data(self):
        return self.model_dump(mode="json")


class WorkerWorldSettingSubject(WorkerWorldSubjectIdentity):
    subject_name: str = Field(alias="subjectName")
    aliases: list[str] = Field(default_factory=list)
    provenance: AnalysisStateProvenance | None = None
    protected_paths: list[WorkerProtectedWorldPath] = Field(default_factory=list, alias="protectedPaths")
    identity_evidence: list[WorkerEvidenceSpan] = Field(default_factory=list, alias="identityEvidence")


class WorkerWorldSettingSubjectPageResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    subjects: list[WorkerWorldSettingSubject]
    page: int = Field(ge=0)
    has_next: bool = Field(alias="hasNext")


class WorkerWorldSettingComparisonContextRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    target_world_setting_ids: list[UUID] = Field(
        alias="targetWorldSettingIds",
        max_length=3,
    )


class WorkerWorldSettingComparisonBatchContextRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    target_world_setting_ids: list[UUID] = Field(
        alias="targetWorldSettingIds",
        max_length=20,
    )
    provisional_subject_keys: list[str] = Field(default_factory=list, alias="provisionalSubjectKeys", max_length=20)


class WorkerWorldSettingProperty(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope_name: str | None = Field(default=None, alias="scopeName")
    setting_name: str = Field(alias="settingName")
    value: str
    provenance: AnalysisStateProvenance | None = None


class WorkerWorldSettingComparisonTarget(WorkerWorldSubjectIdentity):
    model_config = ConfigDict(populate_by_name=True)

    subject_name: str = Field(alias="subjectName")
    properties: list[WorkerWorldSettingProperty]
    version: int = Field(ge=0)
    protected_paths: list[WorkerProtectedWorldPath] = Field(default_factory=list, alias="protectedPaths")


class WorkerWorldSettingComparisonContextResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate: WorkerWorldSettingCandidatePayload
    exact_target_world_setting_id: UUID | None = Field(
        default=None,
        alias="exactTargetWorldSettingId",
    )
    targets: list[WorkerWorldSettingComparisonTarget] = Field(max_length=3)


class WorkerWorldSettingComparisonExactTarget(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_ref: str = Field(alias="candidateRef", pattern=r"^C[1-9][0-9]*$")
    world_setting_id: UUID | None = Field(default=None, alias="worldSettingId")
    provisional_subject_key: str | None = Field(default=None, alias="provisionalSubjectKey")

    @model_validator(mode="after")
    def validate_target_identity(self):
        if self.world_setting_id is not None and self.provisional_subject_key is not None:
            raise ValueError("Exact targets cannot mix actual and provisional identities.")
        return self


class WorkerWorldSettingComparisonBatchContextResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    comparison_batch_id: UUID = Field(alias="comparisonBatchId")
    candidates: list[WorkerWorldSettingComparisonBatchCandidate] = Field(
        min_length=1,
        max_length=20,
    )
    exact_targets: list[WorkerWorldSettingComparisonExactTarget] = Field(alias="exactTargets")
    targets: list[WorkerWorldSettingComparisonTarget] = Field(max_length=20)
    analysis_context: WorkerAnalysisContext | None = Field(default=None, alias="analysisContext")
    context_token: str | None = Field(default=None, alias="contextToken", pattern=r"^[0-9a-f]{64}$")


class WorkerWorldSettingContextVersion(WorkerWorldSubjectIdentity):
    model_config = ConfigDict(populate_by_name=True)

    version: int = Field(ge=0)


class WorkerWorldSettingComparisonCompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    target_world_setting_id: UUID | None = Field(default=None, alias="targetWorldSettingId")
    matched_scope_name: str | None = Field(default=None, alias="matchedScopeName")
    matched_property_name: str | None = Field(default=None, alias="matchedPropertyName")
    consolidation_status: WorldSettingConsolidationStatus = Field(alias="consolidationStatus")
    suggested_operation: WorldSettingOperation = Field(alias="suggestedOperation")
    comparison_review_reason: WorldSettingComparisonReviewReason | None = Field(
        default=None,
        alias="comparisonReviewReason",
    )
    proposed_scope_name: str | None = Field(
        default=None,
        alias="proposedScopeName",
        max_length=100,
    )
    proposed_setting_name: str = Field(alias="proposedSettingName", min_length=1, max_length=100)
    proposed_value: str = Field(alias="proposedValue", min_length=1)
    comparison_reason: str = Field(alias="comparisonReason", min_length=1)
    exact_target_world_setting_id: UUID | None = Field(
        default=None,
        alias="exactTargetWorldSettingId",
    )
    context_versions: list[WorkerWorldSettingContextVersion] = Field(
        alias="contextVersions",
        max_length=3,
    )
    raw_comparison_json: dict[str, Any] | None = Field(
        default=None,
        alias="rawComparisonJson",
    )


class WorkerWorldSettingComparisonBatchDecision(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    decision_ref: str = Field(alias="decisionRef", pattern=r"^D[1-9][0-9]*$")
    source_candidate_refs: list[str] = Field(
        alias="sourceCandidateRefs",
        min_length=1,
        max_length=20,
    )
    existing_root_property_names_to_move: list[str] = Field(
        default_factory=list,
        alias="existingRootPropertyNamesToMove",
        max_length=20,
    )
    canonical_subject_name: str = Field(
        alias="canonicalSubjectName",
        min_length=1,
        max_length=100,
    )
    target_world_setting_id: UUID | None = Field(default=None, alias="targetWorldSettingId")
    provisional_subject_key: str | None = Field(default=None, alias="provisionalSubjectKey")
    matched_scope_name: str | None = Field(default=None, alias="matchedScopeName")
    matched_property_name: str | None = Field(default=None, alias="matchedPropertyName")
    consolidation_status: WorldSettingConsolidationStatus = Field(alias="consolidationStatus")
    suggested_operation: WorldSettingOperation = Field(alias="suggestedOperation")
    comparison_review_reason: WorldSettingComparisonReviewReason | None = Field(
        default=None,
        alias="comparisonReviewReason",
    )
    proposed_scope_name: str | None = Field(default=None, alias="proposedScopeName")
    proposed_setting_name: str = Field(alias="proposedSettingName", min_length=1, max_length=100)
    proposed_value: str = Field(alias="proposedValue", min_length=1)
    comparison_reason: str = Field(alias="comparisonReason", min_length=1)
    raw_comparison_json: dict[str, Any] | None = Field(
        default=None,
        alias="rawComparisonJson",
    )

    @model_validator(mode="after")
    def validate_target_identity(self):
        if self.target_world_setting_id is not None and self.provisional_subject_key is not None:
            raise ValueError("Decisions cannot mix actual and provisional identities.")
        return self


class WorkerWorldSettingDiagnosticProperty(WorkerWorldSubjectIdentity):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    world_setting_id: UUID | None = Field(default=None, alias="targetWorldSettingId")
    scope_name: str | None = Field(default=None, alias="scopeName", max_length=100)
    property_name: str = Field(alias="propertyName", min_length=1, max_length=100)


class WorkerWorldSettingComparisonDiagnostic(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    attempt: int = Field(ge=1, le=30)
    rule: str = Field(pattern=r"^[A-Z][A-Z0-9_]*$", max_length=100)
    stage: Literal[
        "RESPONSE_SCHEMA", "PROPERTY_SELECTION", "DECISION_VALIDATION",
        "SCOPE_PLAN", "PROJECTED_SCOPE_PLAN",
    ] | None = None
    phase: Literal["BATCH", "RECOVERY"] | None = None
    candidate_refs: list[str] = Field(default_factory=list, alias="candidateRefs", max_length=20)
    selected_properties: list[WorkerWorldSettingDiagnosticProperty] = Field(
        default_factory=list, alias="selectedProperties", max_length=20,
    )


class WorkerWorldSettingComparisonBatchFailure(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="forbid")

    source_candidate_refs: list[str] = Field(alias="sourceCandidateRefs", min_length=1, max_length=20)
    failure_code: AnalysisFailureCode = Field(alias="failureCode")
    error_message: str = Field(alias="errorMessage", min_length=1, max_length=1000)
    diagnostics: list[WorkerWorldSettingComparisonDiagnostic] = Field(default_factory=list, max_length=30)


class WorkerWorldSettingComparisonBatchCompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    context_token: str | None = Field(default=None, alias="contextToken", pattern=r"^[0-9a-f]{64}$")

    context_versions: list[WorkerWorldSettingContextVersion] = Field(
        alias="contextVersions",
        max_length=20,
    )
    decisions: list[WorkerWorldSettingComparisonBatchDecision] = Field(
        min_length=0,
        max_length=20,
    )
    failures: list[WorkerWorldSettingComparisonBatchFailure] | None = Field(default=None, max_length=20)
    diagnostics: list[WorkerWorldSettingComparisonDiagnostic] | None = Field(default=None, max_length=30)
    raw_comparison_json: dict[str, Any] | None = Field(
        default=None,
        alias="rawComparisonJson",
    )

    @model_validator(mode="after")
    def validate_result_coverage_shape(self):
        if not self.decisions and not self.failures:
            raise ValueError("A world batch completion must include decisions or failures.")
        return self


class WorkerWorldSettingComparisonFailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    failure_code: AnalysisFailureCode = Field(alias="failureCode")
    error_message: str = Field(alias="errorMessage", min_length=1, max_length=1000)
    diagnostics: list[WorkerWorldSettingComparisonDiagnostic] | None = Field(default=None, max_length=30)
    source_error_code: str | None = Field(
        default=None,
        alias="sourceErrorCode",
        max_length=100,
        pattern=r"^[A-Z][A-Z0-9_]*$",
    )
    source_reason_code: WorldSettingComparisonValidationReason | None = Field(
        default=None,
        alias="sourceReasonCode",
    )


class WorkerCharacterFactComparisonClaimPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")


class WorkerCharacterFactComparisonBatchCandidate(BaseModel):
    """One ordered source candidate in a character comparison batch."""

    model_config = ConfigDict(populate_by_name=True)

    candidate_ref: str = Field(
        alias="candidateRef",
        pattern=r"^C[1-9][0-9]*$",
        max_length=20,
    )
    projected_snapshot_ref: str = Field(
        alias="projectedSnapshotRef",
        pattern=r"^Q[1-9][0-9]*$",
        max_length=20,
    )
    source_episode_no: int | None = Field(default=None, alias="sourceEpisodeNo", ge=1)
    source: WorkerAnalysisSource | None = None
    attribute_value: str | None = Field(default=None, alias="attributeValue")
    value_json: Any | None = Field(default=None, alias="valueJson")
    value_type: SettingValueType = Field(alias="valueType")
    evidence_spans: list[WorkerEvidenceSpan] = Field(
        default_factory=list,
        alias="evidenceSpans",
    )
    raw_fact_key: str = Field(alias="rawFactKey", min_length=1, max_length=150)
    initial_canonical_fact_key: str = Field(
        alias="initialCanonicalFactKey",
        min_length=1,
        max_length=150,
    )
    canonical_key_resolution: Literal["EXACT", "ALIAS", "PATTERN"] = Field(
        alias="canonicalKeyResolution"
    )
    confidence: float | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_source(self):
        if self.source is not None:
            if self.source.source_kind == AnalysisSourceKind.SETTING_BOOK and self.source_episode_no is not None:
                raise ValueError("Book comparison candidates cannot have an episode number.")
            if self.source.source_kind == AnalysisSourceKind.EPISODE and self.source.episode_no != self.source_episode_no:
                raise ValueError("Comparison candidate episode does not match its source.")
        return self


class WorkerCharacterFactComparisonBatchPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    comparison_batch_id: UUID = Field(alias="comparisonBatchId")
    work_id: UUID = Field(alias="workId")
    source_episode_id: UUID | None = Field(default=None, alias="sourceEpisodeId")
    source: WorkerAnalysisSource | None = None
    character_ref: str = Field(alias="characterRef", pattern=r"^K[1-9][0-9]*$")
    matched_character_name: str = Field(alias="matchedCharacterName", min_length=1, max_length=100)
    canonical_fact_type: str = Field(alias="canonicalFactType", min_length=1, max_length=30)
    analysis_context: WorkerAnalysisContext | None = Field(default=None, alias="analysisContext")
    provisional_subject_key: str | None = Field(default=None, alias="provisionalSubjectKey")
    candidates: list[WorkerCharacterFactComparisonBatchCandidate] = Field(
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_group_and_refs(self) -> "WorkerCharacterFactComparisonBatchPayload":
        if self.source is not None:
            if self.source.episode_id != self.source_episode_id:
                raise ValueError("Character batch episode does not match its source.")
            if any(candidate.source is None or candidate.source.identity() != self.source.identity()
                   for candidate in self.candidates):
                raise ValueError("Character batch candidates must share the frozen source.")
            if self.source.source_kind == AnalysisSourceKind.SETTING_BOOK and self.analysis_context is not None and (
                self.source.content_hash != self.analysis_context.source_hash
            ):
                raise ValueError("Book batch source hash does not match its ordered input.")
        elif any(candidate.source is not None and candidate.source.source_kind == AnalysisSourceKind.SETTING_BOOK
                 for candidate in self.candidates):
            raise ValueError("Book candidates require explicit batch source metadata.")
        for index, candidate in enumerate(self.candidates):
            _validate_status_active_value(
                self.canonical_fact_type,
                candidate.value_json,
                f"candidates[{index}].valueJson",
            )
        candidate_refs = [candidate.candidate_ref for candidate in self.candidates]
        projected_refs = [candidate.projected_snapshot_ref for candidate in self.candidates]
        if len(candidate_refs) != len(set(candidate_refs)):
            raise ValueError("Character batch candidate refs must be unique.")
        if len(projected_refs) != len(set(projected_refs)):
            raise ValueError("Character batch projected snapshot refs must be unique.")
        candidate_indexes = [int(reference[1:]) for reference in candidate_refs]
        projected_indexes = [int(reference[1:]) for reference in projected_refs]
        if candidate_indexes != projected_indexes:
            raise ValueError("Each Cn candidate must own the corresponding Qn slot.")
        if candidate_indexes != sorted(candidate_indexes):
            raise ValueError("Character batch candidates must follow local ref chronology.")
        return self


class WorkerCharacterFactComparisonBatchSnapshotEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    snapshot_ref: str = Field(
        alias="snapshotRef",
        pattern=r"^P[1-9][0-9]*$",
        max_length=20,
    )
    origin: Literal["PERSISTED", "PRIOR_DECISION", "PROVISIONAL"] = "PERSISTED"
    provenance: AnalysisStateProvenance | None = None
    source_candidate_ref: str | None = Field(
        default=None,
        alias="sourceCandidateRef",
        pattern=r"^C[1-9][0-9]*$",
    )
    dependency_candidate_refs: list[
        Annotated[str, Field(pattern=r"^C[1-9][0-9]*$", max_length=20)]
    ] = Field(
        default_factory=list,
        alias="dependencyCandidateRefs",
        max_length=20,
    )
    fact_type: str = Field(alias="factType", min_length=1, max_length=30)
    fact_key: str = Field(alias="factKey", min_length=1, max_length=150)
    fact_value: str | None = Field(default=None, alias="factValue")
    value_json: Any | None = Field(default=None, alias="valueJson")


class WorkerCharacterFactComparisonBatchContextResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    comparison_batch_id: UUID = Field(alias="comparisonBatchId")
    character_ref: str = Field(alias="characterRef", pattern=r"^K[1-9][0-9]*$")
    matched_character_name: str = Field(alias="matchedCharacterName", min_length=1, max_length=100)
    canonical_fact_type: str = Field(alias="canonicalFactType", min_length=1, max_length=30)
    analysis_context: WorkerAnalysisContext | None = Field(default=None, alias="analysisContext")
    provisional_subject_key: str | None = Field(default=None, alias="provisionalSubjectKey")
    base_snapshot_version: int = Field(alias="baseSnapshotVersion", ge=0)
    protected_slots: list[WorkerProtectedCharacterSlot] = Field(default_factory=list, alias="protectedSlots")
    candidates: list[WorkerCharacterFactComparisonBatchCandidate] = Field(
        min_length=1,
        max_length=20,
    )
    snapshot_entries: list[WorkerCharacterFactComparisonBatchSnapshotEntry] = Field(
        default_factory=list,
        alias="snapshotEntries",
    )
    context_token: str = Field(alias="contextToken", min_length=64, max_length=64)

    @model_validator(mode="after")
    def validate_status_values(self) -> "WorkerCharacterFactComparisonBatchContextResponse":
        if self.analysis_context is None and len(self.snapshot_entries) > 30:
            raise ValueError("Confirmed-only character snapshots retain the legacy limit of 30.")
        if self.analysis_context is None and any(entry.origin == "PROVISIONAL"
                                                for entry in self.snapshot_entries):
            raise ValueError("Provisional snapshots require an ordered input context.")
        for index, candidate in enumerate(self.candidates):
            _validate_status_active_value(
                self.canonical_fact_type,
                candidate.value_json,
                f"candidates[{index}].valueJson",
            )
        for index, entry in enumerate(self.snapshot_entries):
            _validate_status_active_value(
                entry.fact_type,
                entry.value_json,
                f"snapshotEntries[{index}].valueJson",
            )
        return self


class WorkerCharacterFactComparisonBatchDecision(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_ref: str = Field(
        alias="candidateRef",
        pattern=r"^C[1-9][0-9]*$",
        max_length=20,
    )
    operation: CharacterFactComparisonOperation
    resolved_canonical_fact_key: str = Field(
        alias="resolvedCanonicalFactKey",
        min_length=1,
        max_length=150,
    )
    target_snapshot_ref: str | None = Field(
        default=None,
        alias="targetSnapshotRef",
        pattern=r"^(?:P|Q)[1-9][0-9]*$",
        max_length=20,
    )
    dependency_candidate_refs: list[
        Annotated[str, Field(pattern=r"^C[1-9][0-9]*$", max_length=20)]
    ] = Field(
        default_factory=list,
        alias="dependencyCandidateRefs",
        max_length=20,
    )
    removed_snapshot_refs: list[
        Annotated[str, Field(pattern=r"^(?:P|Q)[1-9][0-9]*$", max_length=20)]
    ] = Field(
        default_factory=list,
        alias="removedSnapshotRefs",
        max_length=30,
    )
    proposed_fact_value: str | None = Field(default=None, alias="proposedFactValue")
    proposed_value_json: Any | None = Field(default=None, alias="proposedValueJson")
    temporal_scope: CharacterFactTemporalScope = Field(alias="temporalScope")
    effective_from_episode_no: int | None = Field(
        default=None, alias="effectiveFromEpisodeNo", ge=1, strict=True, exclude_if=lambda value: value is None,
    )
    comparison_reason: str = Field(
        alias="comparisonReason",
        min_length=1,
        max_length=2000,
    )
    raw_comparison_json: dict[str, Any] | None = Field(
        default=None,
        alias="rawComparisonJson",
    )


class WorkerCharacterFactComparisonBatchFailure(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_ref: str = Field(
        alias="candidateRef",
        pattern=r"^C[1-9][0-9]*$",
        max_length=20,
    )
    failure_code: AnalysisFailureCode = Field(alias="failureCode")
    error_message: str = Field(alias="errorMessage", min_length=1, max_length=1000)


class WorkerCharacterFactComparisonBatchCompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    context_token: str = Field(alias="contextToken", min_length=64, max_length=64)
    decisions: list[WorkerCharacterFactComparisonBatchDecision] = Field(
        default_factory=list,
        max_length=20,
    )
    failures: list[WorkerCharacterFactComparisonBatchFailure] = Field(
        default_factory=list,
        max_length=20,
    )
    raw_comparison_json: dict[str, Any] | None = Field(
        default=None,
        alias="rawComparisonJson",
    )

    @model_validator(mode="after")
    def validate_unique_coverage(self) -> "WorkerCharacterFactComparisonBatchCompleteRequest":
        refs = [decision.candidate_ref for decision in self.decisions] + [
            failure.candidate_ref for failure in self.failures
        ]
        if len(refs) != len(set(refs)):
            raise ValueError("A character batch candidate may be completed only once.")
        if not refs:
            raise ValueError("A character batch completion must contain a result.")
        if len(refs) > 20:
            raise ValueError("A character batch completion may cover at most 20 candidates.")
        return self


class WorkerCharacterFactComparisonBatchFailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    failure_code: AnalysisFailureCode = Field(alias="failureCode")
    error_message: str = Field(alias="errorMessage", min_length=1, max_length=1000)


class WorkerCharacterFactComparisonCandidatePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    work_id: UUID = Field(alias="workId")
    # 마이그레이션 이전 후보나 사용자 입력 후보는 원본 회차가 없을 수 있다.
    source_episode_id: UUID | None = Field(default=None, alias="sourceEpisodeId")
    source: WorkerAnalysisSource | None = None
    entity_name: str = Field(alias="entityName", min_length=1, max_length=100)
    attribute_name: str = Field(alias="attributeName", min_length=1, max_length=100)
    attribute_value: str | None = Field(default=None, alias="attributeValue")
    value_json: Any | None = Field(default=None, alias="valueJson")
    value_type: SettingValueType | None = Field(default=None, alias="valueType")
    evidence_spans: list[WorkerEvidenceSpan] = Field(
        default_factory=list,
        alias="evidenceSpans",
    )
    matched_character_id: UUID = Field(alias="matchedCharacterId")
    matched_character_name: str = Field(alias="matchedCharacterName", min_length=1, max_length=100)
    canonical_fact_type: str = Field(alias="canonicalFactType", min_length=1, max_length=30)
    canonical_fact_key: str = Field(alias="canonicalFactKey", min_length=1, max_length=150)
    confidence: float | None = Field(default=None, ge=0, le=1)


class WorkerCharacterSnapshotEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    fact_type: str = Field(alias="factType", min_length=1, max_length=30)
    fact_key: str = Field(alias="factKey", min_length=1, max_length=150)
    fact_value: str | None = Field(default=None, alias="factValue")
    value_json: Any | None = Field(default=None, alias="valueJson")
    provenance: AnalysisStateProvenance | None = None


class WorkerCharacterPriorFactCandidate(BaseModel):
    """같은 batch에서 현재 후보보다 먼저 나온 동일 Fact slot의 미확정 후보다."""

    model_config = ConfigDict(populate_by_name=True)

    source_episode_no: int | None = Field(default=None, alias="sourceEpisodeNo")
    source: WorkerAnalysisSource | None = None
    attribute_name: str = Field(alias="attributeName", min_length=1, max_length=100)
    attribute_value: str | None = Field(default=None, alias="attributeValue")
    value_json: Any | None = Field(default=None, alias="valueJson")
    evidence_spans: list[WorkerEvidenceSpan] = Field(
        default_factory=list,
        alias="evidenceSpans",
    )
    comparison_status: CharacterFactComparisonStatus = Field(alias="comparisonStatus")
    suggested_operation: CharacterFactComparisonOperation | None = Field(
        default=None,
        alias="suggestedOperation",
    )
    proposed_fact_value: str | None = Field(default=None, alias="proposedFactValue")
    proposed_value_json: Any | None = Field(default=None, alias="proposedValueJson")


class WorkerCharacterFactComparisonContextResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate: WorkerCharacterFactComparisonCandidatePayload
    snapshot_entries: list[WorkerCharacterSnapshotEntry] = Field(alias="snapshotEntries")
    protected_slots: list[WorkerProtectedCharacterSlot] = Field(default_factory=list, alias="protectedSlots")
    prior_candidates: list[WorkerCharacterPriorFactCandidate] = Field(
        default_factory=list,
        alias="priorCandidates",
        max_length=30,
    )
    context_token: str = Field(alias="contextToken", min_length=1, max_length=64)


class WorkerRemovedSnapshotEntry(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    fact_type: str = Field(alias="factType", min_length=1, max_length=30)
    fact_key: str = Field(alias="factKey", min_length=1, max_length=150)


class WorkerCharacterFactComparisonCompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    operation: CharacterFactComparisonOperation
    target_fact_type: str | None = Field(default=None, alias="targetFactType", max_length=30)
    target_fact_key: str | None = Field(default=None, alias="targetFactKey", max_length=150)
    removed_snapshot_entries: list[WorkerRemovedSnapshotEntry] = Field(
        default_factory=list,
        alias="removedSnapshotEntries",
        max_length=30,
    )
    proposed_fact_value: str | None = Field(
        default=None,
        alias="proposedFactValue",
    )
    proposed_value_json: dict[str, Any] | None = Field(
        default=None,
        alias="proposedValueJson",
    )
    temporal_scope: CharacterFactTemporalScope = Field(alias="temporalScope")
    effective_from_episode_no: int | None = Field(
        default=None, alias="effectiveFromEpisodeNo", ge=1, strict=True, exclude_if=lambda value: value is None,
    )
    comparison_reason: str = Field(alias="comparisonReason", min_length=1)
    context_token: str = Field(alias="contextToken", min_length=1, max_length=64)
    raw_comparison_json: dict[str, Any] | None = Field(
        default=None,
        alias="rawComparisonJson",
    )


class WorkerCharacterFactComparisonFailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    failure_code: AnalysisFailureCode = Field(alias="failureCode")
    error_message: str = Field(alias="errorMessage", min_length=1, max_length=1000)
