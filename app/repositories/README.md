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

상태 변경이 도메인 규칙에 가까운 경우에는 모델 메서드에 둡니다. 예를 들어 `AnalysisJob`의 `mark_running`, `mark_failed` 같은 메서드는 Repository가 아니라 도메인 모델이 담당합니다.

## 현재 파일

- `analysis_job_repository.py`
  - `analysis_jobs` 조회를 담당합니다.
  - 존재하지 않는 작업이면 `AppException`을 발생시킵니다.
  - 실행 상태 변경 자체는 `AnalysisJob` 도메인 메서드가 담당합니다.
- `episode_chunk_repository.py`
  - 회차별 청크 조회를 담당합니다.
  - 회차 기준 기존 청크 삭제를 담당합니다.
  - 새로 생성된 청크 목록 저장을 담당합니다.
- `episode_repository.py`
  - 회차 메타데이터 조회를 담당합니다.
  - S3 원문 key를 읽기 위한 `Episode` 조회에 사용합니다.

## 예정 Repository

- `UploadFileRepository`
  - 업로드 파일 메타데이터와 감지된 회차 범위 조회를 담당할 예정입니다.
- `SettingCandidateRepository`
  - AI가 추출한 사용자 검토 전 설정 후보 저장을 담당할 예정입니다.
- `RagEmbeddingTargetRepository`
  - 임베딩 대상 저장 또는 검색 흐름을 담당할 예정입니다.
