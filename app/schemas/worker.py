from datetime import datetime
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.enums import (
    AnalysisFailureCode,
    AnalysisJobCheckpointStage,
    AnalysisJobType,
    CharacterFactComparisonOperation,
    CharacterFactComparisonStatus,
    CharacterFactTemporalScope,
    EpisodeProcessingStatus,
    SettingValueType,
    WorldSettingCategory,
    WorldSettingComparisonReviewReason,
    WorldSettingComparisonValidationReason,
    WorldSettingConsolidationStatus,
    WorldSettingOperation,
    WorldSettingSubjectResolutionType,
)

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


# Spring이 Worker에게 내려주는 캐릭터별 활성 STATUS 최소 문맥 DTO
class WorkerAnalysisActiveCharacterStatusPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    fact_key: str = Field(alias="factKey", min_length=1, max_length=150)
    # provenance가 없는 legacy snapshot은 표시값이 없을 수 있으며 Worker가 값을 합성하지 않는다.
    fact_value: str | None = Field(alias="factValue")


# Spring이 Worker에게 내려주는 기존 캐릭터 정보 DTO
class WorkerAnalysisKnownCharacterPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    character_id: UUID = Field(alias="characterId")
    name: str
    active_statuses: list[WorkerAnalysisActiveCharacterStatusPayload] = Field(
        default_factory=list,
        alias="activeStatuses",
    )


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


class WorkerEvidenceSpan(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    quote: str = Field(min_length=1)
    start_offset: int | None = Field(default=None, alias="startOffset", ge=0)
    end_offset: int | None = Field(default=None, alias="endOffset", ge=0)


class WorkerWorldSettingCandidatePublishItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    category: WorldSettingCategory
    subject_name: str = Field(alias="subjectName", min_length=1, max_length=100)
    scope_name: str | None = Field(default=None, alias="scopeName", max_length=100)
    setting_name: str = Field(alias="settingName", min_length=1, max_length=100)
    extracted_value: str = Field(alias="extractedValue", min_length=1)
    evidence_spans: list[WorkerEvidenceSpan] = Field(alias="evidenceSpans", min_length=1)
    extraction_confidence: Literal[0.65, 0.8, 0.95] = Field(alias="extractionConfidence")
    raw_extraction_json: dict[str, Any] | None = Field(
        default=None,
        alias="rawExtractionJson",
    )


class WorkerWorldSettingCandidatePublishRequest(BaseModel):
    candidates: list[WorkerWorldSettingCandidatePublishItem]


class WorkerWorldSettingCandidatePayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    work_id: UUID = Field(alias="workId")
    source_episode_id: UUID = Field(alias="sourceEpisodeId")
    category: WorldSettingCategory
    subject_name: str = Field(alias="subjectName")
    scope_name: str | None = Field(default=None, alias="scopeName")
    setting_name: str = Field(alias="settingName")
    extracted_value: str = Field(alias="extractedValue")
    evidence_spans: list[WorkerEvidenceSpan] = Field(alias="evidenceSpans")
    extraction_confidence: float | None = Field(default=None, alias="extractionConfidence")


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


class WorkerWorldSettingSubjectResolutionCandidate(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    source_episode_id: UUID = Field(alias="sourceEpisodeId")
    category: WorldSettingCategory
    subject_name: str = Field(alias="subjectName", min_length=1, max_length=100)


class WorkerWorldSettingSubjectResolutionPendingResponse(BaseModel):
    candidates: list[WorkerWorldSettingSubjectResolutionCandidate]


class WorkerWorldSettingSubjectResolutionRequestItem(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    target_world_setting_ids: list[UUID] = Field(
        alias="targetWorldSettingIds",
        max_length=20,
    )


class WorkerWorldSettingSubjectResolutionRequest(BaseModel):
    resolutions: list[WorkerWorldSettingSubjectResolutionRequestItem] = Field(min_length=1)


class WorkerWorldSettingSubjectResolutionResult(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    candidate_id: UUID = Field(alias="candidateId")
    resolution_type: WorldSettingSubjectResolutionType = Field(alias="resolutionType")
    canonical_subject_key: str = Field(alias="canonicalSubjectKey", min_length=1)
    canonical_subject_name: str = Field(alias="canonicalSubjectName", min_length=1)
    target_world_setting_ids: list[UUID] = Field(alias="targetWorldSettingIds", max_length=20)


class WorkerWorldSettingSubjectResolutionResponse(BaseModel):
    resolutions: list[WorkerWorldSettingSubjectResolutionResult]


class WorkerWorldSettingComparisonBatchPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    comparison_batch_id: UUID = Field(alias="comparisonBatchId")
    work_id: UUID = Field(alias="workId")
    source_episode_id: UUID = Field(alias="sourceEpisodeId")
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
    raw_scope_name: str | None = Field(default=None, alias="rawScopeName")
    candidates: list[WorkerWorldSettingComparisonBatchCandidate] = Field(
        min_length=1,
        max_length=20,
    )


class WorkerWorldSettingSubject(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    world_setting_id: UUID = Field(alias="worldSettingId")
    subject_name: str = Field(alias="subjectName")


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


class WorkerWorldSettingProperty(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    scope_name: str | None = Field(default=None, alias="scopeName")
    setting_name: str = Field(alias="settingName")
    value: str


class WorkerWorldSettingComparisonTarget(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    world_setting_id: UUID = Field(alias="worldSettingId")
    subject_name: str = Field(alias="subjectName")
    properties: list[WorkerWorldSettingProperty]
    version: int = Field(ge=0)


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


class WorkerWorldSettingComparisonBatchContextResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    comparison_batch_id: UUID = Field(alias="comparisonBatchId")
    candidates: list[WorkerWorldSettingComparisonBatchCandidate] = Field(
        min_length=1,
        max_length=20,
    )
    exact_targets: list[WorkerWorldSettingComparisonExactTarget] = Field(alias="exactTargets")
    targets: list[WorkerWorldSettingComparisonTarget] = Field(max_length=20)


class WorkerWorldSettingContextVersion(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    world_setting_id: UUID = Field(alias="worldSettingId")
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


class WorkerWorldSettingComparisonBatchCompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    context_versions: list[WorkerWorldSettingContextVersion] = Field(
        alias="contextVersions",
        max_length=20,
    )
    decisions: list[WorkerWorldSettingComparisonBatchDecision] = Field(
        min_length=1,
        max_length=20,
    )
    raw_comparison_json: dict[str, Any] | None = Field(
        default=None,
        alias="rawComparisonJson",
    )


class WorkerWorldSettingComparisonFailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    failure_code: AnalysisFailureCode = Field(alias="failureCode")
    error_message: str = Field(alias="errorMessage", min_length=1, max_length=1000)
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


class WorkerCharacterFactComparisonBatchPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    comparison_batch_id: UUID = Field(alias="comparisonBatchId")
    work_id: UUID = Field(alias="workId")
    source_episode_id: UUID | None = Field(default=None, alias="sourceEpisodeId")
    character_ref: str = Field(alias="characterRef", pattern=r"^K[1-9][0-9]*$")
    matched_character_name: str = Field(alias="matchedCharacterName", min_length=1, max_length=100)
    canonical_fact_type: str = Field(alias="canonicalFactType", min_length=1, max_length=30)
    candidates: list[WorkerCharacterFactComparisonBatchCandidate] = Field(
        min_length=1,
        max_length=20,
    )

    @model_validator(mode="after")
    def validate_group_and_refs(self) -> "WorkerCharacterFactComparisonBatchPayload":
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
    origin: Literal["PERSISTED", "PRIOR_DECISION"] = "PERSISTED"
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
    base_snapshot_version: int = Field(alias="baseSnapshotVersion", ge=0)
    candidates: list[WorkerCharacterFactComparisonBatchCandidate] = Field(
        min_length=1,
        max_length=20,
    )
    snapshot_entries: list[WorkerCharacterFactComparisonBatchSnapshotEntry] = Field(
        default_factory=list,
        alias="snapshotEntries",
        max_length=30,
    )
    context_token: str = Field(alias="contextToken", min_length=64, max_length=64)


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


class WorkerCharacterPriorFactCandidate(BaseModel):
    """같은 batch에서 현재 후보보다 먼저 나온 동일 Fact slot의 미확정 후보다."""

    model_config = ConfigDict(populate_by_name=True)

    source_episode_no: int | None = Field(default=None, alias="sourceEpisodeNo")
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
