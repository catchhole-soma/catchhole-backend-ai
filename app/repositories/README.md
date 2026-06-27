# repositories

DB 조회와 저장을 담당하는 Repository 계층입니다.

Spring 기준으로는 `JpaRepository` 또는 Repository 계층에 가깝습니다.

## 역할

Repository는 SQLAlchemy session을 사용해 DB 접근만 담당합니다.

- DB에서 필요한 데이터를 조회합니다.
- ORM 모델을 저장하거나 삭제합니다.
- 조회 조건과 정렬 조건을 SQLAlchemy 쿼리로 표현합니다.

다음 책임은 Repository에 넣지 않습니다.

- S3 원문 조회
- LLM 호출
- 청킹/분석 판단
- 상태 전이 규칙 결정
- API 요청/응답 변환

분석 작업 상태 변경은 Spring 서버가 담당합니다. Python Worker는 DB row를 직접 바꾸지 않고 Spring 내부 Worker API로 진행률, 완료, 실패를 보고합니다.

## 현재 파일

- `analysis_job_repository.py`
  - `analysis_jobs` 조회를 담당합니다.
  - 존재하지 않는 작업이면 `AppException`을 발생시킵니다.
  - 실행 상태 변경은 담당하지 않습니다.
- `episode_chunk_repository.py`
  - 회차별 청크 조회를 담당합니다.
  - 회차 기준 기존 청크 삭제를 담당합니다.
  - 새로 생성된 청크 목록 저장을 담당합니다.
- `episode_repository.py`
  - 회차 메타데이터 조회를 담당합니다.
  - S3 원문 key를 읽기 위한 `Episode` 조회에 사용합니다.
- `setting_candidate_repository.py`
  - AI가 추출한 사용자 검토 전 설정 후보 저장을 담당합니다.
  - 같은 `analysis_job_id` 재실행 시 후보가 중복 저장되지 않도록 기존 후보 삭제 쿼리를 제공합니다.

## 예정 Repository

- `UploadFileRepository`
  - 업로드 파일 메타데이터와 감지된 회차 범위 조회를 담당할 예정입니다.
- `RagEmbeddingTargetRepository`
  - 임베딩 대상 저장 또는 검색 흐름을 담당할 예정입니다.
