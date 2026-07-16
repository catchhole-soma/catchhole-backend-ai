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
  - 보조 상태 조회 API에서 분석 작업 상태, 현재 단계, 실패 사유와 요약 정보를 읽습니다.
  - 진행·완료·실패 상태 변경은 Python이 DB에 직접 반영하지 않고 Spring 내부 Worker API로 보고합니다.
- `episode.py`
  - Spring의 `episodes` 테이블과 매핑됩니다.
  - 회차 메타데이터와 S3 원문 key를 읽을 때 사용합니다.
- `episode_chunk.py`
  - 청킹 결과를 저장할 `episode_chunks` 테이블과 매핑됩니다.
  - LLM 입력 단위와 근거 위치 계산에 필요한 offset, 문단 범위를 저장합니다.
  - `vector(1536)` 임베딩과 모델·버전·생성 시각을 저장합니다.
  - 별도 Java Entity 없이 Spring Flyway V1이 테이블을 생성하고 Python SQLAlchemy가 읽고 씁니다.
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

## 후속 확장

NVM-141의 검색 결과는 별도 DB 테이블이나 `RagEmbeddingTarget` 모델을 추가하지 않고 내부 DTO로 반환합니다. `SettingCandidate`와 기존 fact를 조합하는 검증 문맥 객체는 NVM-143에서 검색 인터페이스가 확정된 뒤 정의합니다.
