# mappers

서로 다른 계층의 객체를 변환하는 패키지입니다.

Spring 기준으로는 Mapper 계층에 가깝습니다. Python에서는 클래스, 정적 메서드, 모듈 함수 중 하나를 선택할 수 있지만, 현재 프로젝트에서는 Spring과 비슷하게 Mapper 클래스를 사용합니다.

## 역할

- ORM 모델을 API 응답 schema로 변환합니다.
- 내부 dataclass 결과를 DB 저장용 ORM 모델로 변환합니다.
- 변환 과정에서 DB session, S3, LLM 같은 외부 의존성을 직접 사용하지 않습니다.

다음 책임은 Mapper에 넣지 않습니다.

- DB 조회와 저장
- S3 원문 조회
- 청킹 알고리즘 실행
- LLM 호출
- 트랜잭션 처리

## 현재 파일

- `analysis_job_mapper.py`
  - `AnalysisJob` ORM 모델을 상태 조회 응답으로 변환합니다.
  - `summary_json` 문자열을 dict로 파싱합니다.
- `episode_chunk_mapper.py`
  - `EpisodeChunkDraft`를 `EpisodeChunk` ORM 모델로 변환합니다.
  - `episode_id`는 청킹 함수가 아니라 저장용 변환 단계에서 붙입니다.
- `setting_candidate_mapper.py`
  - LLM 검증을 통과한 `ExtractedSettingCandidate`를 `SettingCandidate` ORM 모델로 변환합니다.
  - `work_id`, `episode_id`, `analysis_job_id`는 Worker/Service 흐름에서 붙입니다.
  - `raw_entity_mention`이 비어 있으면 `entity_name`을 fallback으로 저장합니다.
  - character name resolver 결과를 받아 `matched_character_id`, `match_status`를 함께 저장합니다.

## 기준

`EpisodeChunkDraft`는 DB를 모르는 순수 청킹 결과입니다. `EpisodeChunkMapper`는 여기에 `episode_id`와 선택 metadata를 붙여 `episode_chunks` 저장 구조로 변환합니다.

현재 청크 목록을 Spring API 응답으로 반환하는 흐름은 없으므로, 별도 chunk response schema는 두지 않습니다.
