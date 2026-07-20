# schemas

외부 경계에서 주고받는 JSON 구조를 검증하는 Pydantic schema를 두는 패키지입니다.

Spring 기준으로는 Request/Response DTO에 가깝습니다.

## 역할

- FastAPI endpoint의 response model을 정의합니다.
- Spring 내부 Worker API와 주고받는 payload 구조를 정의합니다.
- snake_case Python 필드와 camelCase JSON 필드 사이의 alias를 관리합니다.
- 외부에서 들어오거나 외부로 나가는 값의 최소 검증 규칙을 둡니다.

다음 책임은 Schemas에 넣지 않습니다.

- DB 테이블 매핑
- SQLAlchemy query
- LLM prompt 본문
- 내부 알고리즘 중간 결과
- 저장용 entity 변환 로직

## 현재 파일

- `analysis.py`
  - FastAPI health/status 응답과 분석 job 상태 조회 응답을 정의합니다.
- `worker.py`
  - Python Worker와 Spring 내부 Worker API 사이에서 사용하는 request/response payload를 정의합니다.
  - `modelName`, `analysisJobId`, `contentS3Key` 같은 Spring JSON 필드를 Python 코드에서는 `model_name`, `analysis_job_id`, `content_s3_key`로 다룰 수 있게 alias를 둡니다.
  - claim payload의 `knownCharacters`는 Python에서 `known_characters`로 받고, 설정 후보 캐릭터명 매칭 resolver에 전달합니다.
  - claim payload의 `characterSettingSchemas`는 Python에서 `character_setting_schemas`로 받습니다. 각 항목은 `schemaKey`, `displayName`, `attributePattern`, `aliases`, `valueType`만 포함하며, 필드가 없는 이전 payload는 빈 목록으로 처리합니다.

## 다른 값 객체와의 구분

이 프로젝트는 값의 용도에 따라 다음 기준을 사용합니다.

| 용도 | 위치 | 예시 |
| --- | --- | --- |
| 외부 JSON 경계 검증 | `app/schemas` | Spring 내부 API payload, FastAPI response |
| 특정 도메인 내부 검증 | 해당 패키지 내부 `schemas.py` | `app/analysis/schemas.py` |
| 내부 계산 결과 | `dataclass` | `EpisodeChunkDraft`, `LlmTextResponse` |
| DB 테이블 매핑 | `app/models` | `EpisodeChunk`, `SettingCandidate` |

즉, `app/schemas`는 외부 계약을 표현하는 곳이고, 내부 알고리즘 결과나 DB 모델을 대신하지 않습니다.
