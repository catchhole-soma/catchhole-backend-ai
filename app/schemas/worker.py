from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Worker가 Spring 서버에 job claim 요청
class WorkerAnalysisJobClaimRequest(BaseModel):
    # Python 필드명과 JSON alias를 둘 다 허용한다, 예: model_name or modelName 모두 가능
    #Pydantic 모델의 설정값, 실제 데이터 필드로 들어가지 않음
    model_config = ConfigDict(populate_by_name=True)

    model_name: str | None = Field(default=None, alias="modelName", max_length=100)
    # Spring에 알려줄 현재 작업 단계
    current_step: str | None = Field(default=None, alias="currentStep", max_length=100)

# Worker가 분석 진행 상황을 Spring에 보고할 때 쓰는 DTO
class WorkerAnalysisJobProgressRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    # 현재 진행 단계, 빈 문자열은 허용 x
    current_step: str = Field(alias="currentStep", min_length=1, max_length=100)

# Worker가 분석 성공을 Spring에 보고할 때 쓰는 DTO
class WorkerAnalysisJobCompleteRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    # 분석 결과 요약 JSON 문자열
    summary_json: str | None = Field(default=None, alias="summaryJson")
    # 입력 토큰 수
    input_token_count: int | None = Field(default=None, alias="inputTokenCount", ge=0)
    # 출력 토큰 수
    output_token_count: int | None = Field(default=None, alias="outputTokenCount", ge=0)

# Worker가 분석 실패를 Spring에 보고할 때 쓰는 DTO
class WorkerAnalysisJobFailRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    # 실패 사유
    error_message: str = Field(alias="errorMessage", min_length=1)

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


# Spring이 Worker에게 내려주는 기존 캐릭터 정보 DTO
class WorkerAnalysisKnownCharacterPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    character_id: UUID = Field(alias="characterId")
    name: str
    aliases: list[str] = Field(default_factory=list)


# Spring이 Worker에게 내려주는 분석 job 전체 payload
class WorkerAnalysisJobPayload(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    analysis_job_id: UUID = Field(alias="analysisJobId")
    job_type: str = Field(alias="jobType")
    work_id: UUID = Field(alias="workId")
    work_title: str = Field(alias="workTitle")
    batch_id: UUID = Field(alias="batchId")
    model_name: str | None = Field(default=None, alias="modelName")
    current_step: str | None = Field(default=None, alias="currentStep")
    known_characters: list[WorkerAnalysisKnownCharacterPayload] = Field(
        default_factory=list,
        alias="knownCharacters",
    )
    episodes: list[WorkerAnalysisEpisodePayload]
