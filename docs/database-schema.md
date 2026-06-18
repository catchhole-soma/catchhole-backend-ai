# Database Schema Draft

이 문서는 CatchHole AI 서버가 참조하는 PostgreSQL 스키마 초안을 추적하기 위한 문서다.

Spring 서버가 인증, 작품/회차 API, 사용자 소유권 검증, 사용자-facing 응답을 담당하고, Python AI 서버는 `analysis_jobs`를 기준으로 원문 로드, 청킹, 설정 후보 추출, 근거 저장, 임베딩 검색을 담당한다.

Python AI 서버는 DB 스키마의 주도권을 갖기보다는 Spring 서버와 공유하는 PostgreSQL 스키마에 맞춰 읽기/쓰기 모델을 가진다. 따라서 이 문서의 스키마 변경 사항은 Spring 엔티티/마이그레이션과 함께 맞춰야 한다.

## Scope For First AI Issue

첫 번째 이슈에서는 전체 ERD를 모두 구현하지 않고, 분석 작업 실행 기반에 필요한 최소 테이블부터 접근한다.

- `analysis_jobs`
- `works`
- `episodes`
- `upload_batches`
- `upload_files`

이후 청킹/추출/검색 작업에서 다음 테이블 접근을 추가한다.

- `episode_chunks`
- `setting_candidates`
- `characters`
- `character_facts`
- `conflict_reports`
- `conflict_report_evidences`

## Current Processing Contract

초기 MVP에서는 Queue 없이 Spring 서버가 Python AI 서버의 분석 실행 API를 직접 호출한다.

```text
Spring API
  -> 사용자 인증과 작품 소유권 검증
  -> episodes / upload_files / analysis_jobs 생성
  -> Python AI 서버 분석 API 호출
  -> Python worker가 analysis_job_id 기준으로 DB 조회
  -> Python worker가 S3 원문 조회
  -> episode_chunks / setting_candidates / evidence 저장
  -> Spring API가 DB에서 진행률과 결과 조회
```

작업 유실, 재시도, 동시성 제어, 장시간 처리 문제가 확인되면 SQS 같은 queue 기반 구조로 전환한다.

## Important Decisions

- `setting_candidates`는 설정집 파일 테이블이 아니다. AI가 원문에서 추출한 사용자 검토 전 후보를 저장한다.
- 설정집 파일은 별도 테이블로 관리할 예정이다.
- 청킹은 Python AI 서버가 담당한다.
- 근거 가시화를 위해 `episode_chunks`는 원문 위치 정보를 반드시 보존한다.
- `source_chunk_id`만으로는 화면에서 정확한 근거를 보여주기 어렵다. 후보 저장 시 `evidence_quote`, `start_offset`, `end_offset`도 함께 저장하는 방향을 우선한다.
- MVP의 기본 벡터 검색 대상은 `episode_chunks.embedding`이다.
- `characters`, `setting_candidates`, `character_facts`는 우선 일반 컬럼과 JSONB 조건으로 조회하고, 의미 기반 원문 검색은 `episode_chunks.embedding`으로 수행한다.

## Open Schema Notes

현재 ERD 초안에서 실제 구현 전 조정이 필요한 후보들이다.

| Area | Current Draft | Proposed Direction | Reason |
| --- | --- | --- | --- |
| `MEMBERSRS` relation typo | `MEMBERSRS ||--o{ WORKS` | `MEMBERS ||--o{ WORKS` | ERD 오타로 보임 |
| `refresh_tokens.user_id` | `user_id` | `member_id` | 인증 PR에서 member 기준으로 구현됨 |
| `works.owner_user_id` | `owner_user_id` | `member_id` 또는 Spring 구현명과 동기화 | Java Work 엔티티와 명칭 통일 필요 |
| `upload_files.detected_episode_no` | 단일 추정 회차 번호 | `detected_episode_start_no`, `detected_episode_end_no`, `detected_episode_count` | 한 파일에 여러 회차가 들어갈 수 있음 |
| `episodes.content_text` | 본문 원문 저장 | S3 key + 필요 시 캐시/요약 컬럼 | 원문은 S3 저장을 우선 검토 중 |
| `analysis_jobs` retry | 없음 | `retry_count`, `last_error_code`, `last_error_message` 검토 | JSON 검증/재시도와 실패 추적 필요 |
| `setting_candidates` evidence | `raw_ai_result_json` 중심 | quote/offset 일부 일반 컬럼 승격 검토 | 근거 화면 표시와 검색 편의 |

## ERD Draft

```mermaid
erDiagram
    MEMBERS ||--o{ REFRESH_TOKENS : has
    MEMBERSRS ||--o{ WORKS : creates
    MEMBERS ||--o{ UPLOAD_BATCHES : uploads
    MEMBERS ||--o{ CHARACTER_CUSTOM_ATTRIBUTES : creates

    WORKS ||--o{ UPLOAD_BATCHES : receives
    UPLOAD_BATCHES ||--o{ UPLOAD_FILES : contains

    WORKS ||--o{ EPISODES : has
    UPLOAD_FILES o|--o| EPISODES : imports_as

    EPISODES ||--o{ EPISODE_CHUNKS : split_into

    WORKS ||--o{ ANALYSIS_JOBS : runs
    UPLOAD_BATCHES o|--o{ ANALYSIS_JOBS : triggers
    EPISODES o|--o{ ANALYSIS_JOBS : targets

    WORKS ||--o{ SETTING_CANDIDATES : has
    EPISODES o|--o{ SETTING_CANDIDATES : extracted_from
    EPISODE_CHUNKS o|--o{ SETTING_CANDIDATES : sourced_from
    ANALYSIS_JOBS o|--o{ SETTING_CANDIDATES : extracted_by

    WORKS ||--o{ CHARACTERS : has
    EPISODES o|--o{ CHARACTERS : first_appears_in

    CHARACTERS ||--o{ CHARACTER_FACTS : owns
    EPISODES o|--o{ CHARACTER_FACTS : sourced_from
    EPISODE_CHUNKS o|--o{ CHARACTER_FACTS : evidence_from
    ANALYSIS_JOBS o|--o{ CHARACTER_FACTS : extracted_by

    CHARACTERS ||--o{ CHARACTER_CUSTOM_ATTRIBUTES : has

    ANALYSIS_JOBS ||--o{ CONFLICT_REPORTS : produces
    WORKS ||--o{ CONFLICT_REPORTS : has
    EPISODES o|--o{ CONFLICT_REPORTS : found_in
    EPISODE_CHUNKS o|--o{ CONFLICT_REPORTS : current_grounded_by

    CONFLICT_REPORTS ||--o{ CONFLICT_REPORT_EVIDENCES : has
    EPISODE_CHUNKS o|--o{ CONFLICT_REPORT_EVIDENCES : evidence_from

    MEMBERS {
      uuid id PK
      string email
      string password_hash
      string phone_number
      boolean phone_verified
      string display_name
      string profile_image_url
      string status
      timestamp created_at
      timestamp updated_at
    }

    PHONE_VERIFICATIONS {
      uuid id PK
      string phone_number
      string code_hash
      timestamp expires_at
      int attempt_count
      timestamp verified_at
      timestamp created_at
    }

    REFRESH_TOKENS {
      uuid id PK
      uuid user_id FK
      string token_hash
      timestamp expires_at
      timestamp revoked_at
      timestamp created_at
    }

    WORKS {
      uuid id PK
      uuid owner_user_id FK
      string title
      string genre
      text description
      string status
      int latest_episode_no
      timestamp created_at
      timestamp updated_at
    }

    UPLOAD_BATCHES {
      uuid id PK
      uuid work_id FK
      uuid uploaded_by FK
      string upload_type
      string source_type
      string status
      int file_count
      timestamp created_at
      timestamp completed_at
    }

    UPLOAD_FILES {
      uuid id PK
      uuid batch_id FK
      string original_filename
      string mime_type
      string storage_url
      int file_size
      int detected_episode_no
      string parse_status
      timestamp created_at
    }

    EPISODES {
      uuid id PK
      uuid work_id FK
      uuid source_file_id FK
      int episode_no
      string title
      text content_text
      string status
      timestamp created_at
      timestamp updated_at
    }

    EPISODE_CHUNKS {
      uuid id PK
      uuid episode_id FK
      int chunk_index
      text chunk_text
      vector embedding
      int start_offset
      int end_offset
      jsonb metadata_json
    }

    ANALYSIS_JOBS {
      uuid id PK
      uuid work_id FK
      uuid batch_id FK
      uuid episode_id FK
      string job_type
      string status
      int progress
      string current_step
      string model_name
      int input_token_count
      int output_token_count
      jsonb summary_json
      timestamp started_at
      timestamp completed_at
      timestamp created_at
    }

    SETTING_CANDIDATES {
      uuid id PK
      uuid work_id FK
      uuid episode_id FK
      uuid source_chunk_id FK
      uuid extracted_by_job_id FK
      string entity_type
      string entity_name
      string attribute_name
      string attribute_value
      string value_type
      jsonb value_json
      float confidence
      string status
      jsonb raw_ai_result_json
      timestamp created_at
      timestamp reviewed_at
    }

    CHARACTERS {
      uuid id PK
      uuid work_id FK
      string name
      string role_label
      int current_age
      int current_level
      jsonb profile_json
      jsonb stats_json
      jsonb skills_json
      jsonb items_json
      jsonb statuses_json
      uuid first_appearance_episode_id FK
      string review_status
      string status
      timestamp created_at
      timestamp updated_at
    }

    CHARACTER_FACTS {
      uuid id PK
      uuid character_id FK
      string fact_type
      string fact_key
      string fact_value
      string normalized_value
      jsonb value_json
      uuid source_episode_id FK
      uuid source_chunk_id FK
      uuid extracted_by_job_id FK
      float confidence
      string review_status
      boolean is_current
      int effective_from_episode_no
      timestamp created_at
    }

    CHARACTER_CUSTOM_ATTRIBUTES {
      uuid id PK
      uuid character_id FK
      string attr_key
      text attr_value
      uuid created_by FK
      timestamp created_at
      timestamp updated_at
    }

    CONFLICT_REPORTS {
      uuid id PK
      uuid analysis_job_id FK
      uuid work_id FK
      uuid episode_id FK
      string conflict_type
      string severity
      string status
      string entity_type
      string entity_name
      text current_sentence
      uuid current_chunk_id FK
      string previous_setting_value
      text previous_evidence_text
      text suggestion
      float confidence
      timestamp created_at
      timestamp updated_at
    }

    CONFLICT_REPORT_EVIDENCES {
      uuid id PK
      uuid report_id FK
      uuid chunk_id FK
      string evidence_type
      text evidence_text
      string setting_value
      float score
      timestamp created_at
    }
```

## Change Log

| Date | Change | Reason |
| --- | --- | --- |
| 2026-06-18 | Added initial ERD draft to AI repo docs. | Track schema decisions before implementing SQLAlchemy models/repositories. |
