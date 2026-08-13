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
  - claim payload는 복수 `episodes`가 아니라 단일 `episode`를 필수로 받으며, 한 `AnalysisJob`의 회차 하나를 표현합니다.
  - claim payload의 `knownCharacters`는 Python에서 `known_characters`로 받고, `characterId`와 이름을 캐릭터명 매칭에 사용합니다. LLM prompt에는 내부 ID를 제외한 대표 이름만 전달합니다.
  - claim payload의 `characterSettingSchemas`는 Python에서 `character_setting_schemas`로 받습니다. 각 항목은 `schemaKey`, `displayName`, `attributePattern`, `aliases`, `valueType`만 포함합니다.
  - 이전 payload를 역직렬화할 수 있도록 필드가 없으면 빈 목록으로 파싱하지만, 빈 목록은 현재 분석 계약과 호환되는 입력이 아닙니다. Worker는 원문·청크·후보를 변경하기 전에 해당 job을 실패 보고해 후보 0개 교체를 막습니다.
  - claim payload의 lease token/만료 시각, claim 횟수, checkpoint와 선택 `worldSettingCandidateId`/`settingCandidateId`를 타입이 지정된 필드로 검증합니다.
  - 세계관 후보 게시, 대상명 페이지, 최대 3개 비교 문맥, context version과 ADD/UPDATE/MERGE/EXCLUDE 완료 요청을 정의합니다.
  - 캐릭터 Fact 비교는 `{candidate, snapshotEntries, contextToken}` 문맥을 받습니다. candidate에는 canonical Fact slot과 원문 근거가 있고, snapshot entry에는 현재값의 canonical slot, 사용자 표시 문자열 `factValue`, 구조화 값 `valueJson`만 포함됩니다. provenance Fact ID는 Spring 내부의 문맥 무결성 검증에만 사용하며 Python에는 노출하지 않습니다.
  - snapshot을 변경하는 비교 결과는 최종 표시 문자열 `proposedFactValue`와 구조화 값 `proposedValueJson`을 함께 Spring에 전달합니다. 이 둘을 분리해야 복합 JSON 설정도 상세 화면에서 동일한 표시값으로 복원할 수 있습니다.
  - 캐릭터 비교 완료 요청은 DB ID나 원문 quote/offset을 되돌려 보내지 않고 operation, canonical target, 제거할 snapshot key, proposed JSON, temporal scope, reason, context token만 전달합니다.
  - Worker DTO의 raw JSON은 `dict`/`list` 경계로 유지하고 Backend 전용 Jackson 객체나 UUID 생성 책임을 Python schema에 넣지 않습니다.

## 다른 값 객체와의 구분

이 프로젝트는 값의 용도에 따라 다음 기준을 사용합니다.

| 용도 | 위치 | 예시 |
| --- | --- | --- |
| 외부 JSON 경계 검증 | `app/schemas` | Spring 내부 API payload, FastAPI response |
| 특정 도메인 내부 검증 | 해당 패키지 내부 `schemas.py` | `app/analysis/schemas.py` |
| 내부 계산 결과 | `dataclass` | `EpisodeChunkDraft`, `LlmTextResponse` |
| DB 테이블 매핑 | `app/models` | `EpisodeChunk`, `SettingCandidate` |

즉, `app/schemas`는 외부 계약을 표현하는 곳이고, 내부 알고리즘 결과나 DB 모델을 대신하지 않습니다.
