# models

Python AI 서버가 PostgreSQL을 직접 읽거나 써야 할 때 사용하는 SQLAlchemy ORM 모델을 둡니다.

## 기준

이 패키지의 모델은 Spring 서버가 관리하는 PostgreSQL 스키마를 따라갑니다.

- Python AI 서버는 사용자 요청의 주 소유자가 아닙니다.
- 사용자 인증, 작품 소유권 검증, 사용자-facing API는 Spring 서버가 담당합니다.
- Python 모델은 Spring/PostgreSQL 컬럼명을 임의로 재정의하지 않습니다.
- Spring 엔티티의 컬럼명이 바뀌면 Python ORM 모델도 함께 맞춥니다.

## 현재 파일

- `base.py`
  - SQLAlchemy ORM 모델들이 공통으로 사용하는 declarative base입니다.
- `mixins.py`
  - `created_at`, `updated_at` 같은 공통 timestamp 컬럼을 정의합니다.
- `analysis_job.py`
  - Spring의 `analysis_jobs` 테이블과 매핑됩니다.
  - 분석 작업 상태, 현재 단계, 실패 사유, 요약 정보를 읽고 갱신할 때 사용합니다.
- `episode.py`
  - Spring의 `episodes` 테이블과 매핑됩니다.
  - 회차 메타데이터와 S3 원문 key를 읽을 때 사용합니다.
- `episode_chunk.py`
  - 청킹 결과를 저장할 `episode_chunks` 테이블과 매핑됩니다.
  - LLM 입력 단위와 근거 위치 계산에 필요한 offset, 문단 범위를 저장합니다.
  - 현재 Spring 쪽에는 아직 대응 엔티티가 없으므로 DB 스키마 반영 협의가 필요합니다.
- `setting_candidate.py`
  - Spring의 `setting_candidates` 테이블과 매핑됩니다.
  - LLM이 추출한 사용자 검토 전 설정 후보를 저장합니다.
  - `evidence_spans[].start_offset`, `end_offset`은 Python Worker가 quote를 다시 찾아 계산한 회차 전체 원문 기준 위치입니다. quote를 찾지 못하면 `null`로 저장될 수 있습니다.
  - `raw_entity_mention`, `matched_character_id`, `match_status`로 LLM 원문 표현과 기존 캐릭터 매칭 결과를 저장합니다.
  - `review_status`는 최초 저장 시 `PENDING_REVIEW`로 둡니다.
- `upload_batch.py`
  - Spring의 `upload_batches` 테이블과 매핑됩니다.
  - 업로드 묶음과 분석 작업의 연결 정보를 읽을 때 사용합니다.
- `upload_file.py`
  - Spring의 `upload_files` 테이블과 매핑됩니다.
  - 업로드 파일의 저장 위치와 파싱 상태를 읽을 때 사용합니다.
- `work.py`
  - Spring의 `works` 테이블과 매핑됩니다.
  - 분석 작업이 어느 작품에 속하는지 확인할 때 사용합니다.

## 예정 모델

- `RagEmbeddingTarget`
  - 임베딩 대상 저장 또는 검색 흐름에 사용할 예정입니다.
