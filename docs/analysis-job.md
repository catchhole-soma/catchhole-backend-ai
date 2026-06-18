# Analysis Job

## 목적

Analysis Job은 Spring 서버가 생성한 분석 작업을 Python AI 서버가 처리하기 위한 실행 단위입니다.

첫 번째 AI 이슈에서는 실제 LLM 호출, 청킹, 임베딩, 후보 저장을 모두 구현하지 않고, Python AI 서버가 `analysis_job_id`를 기준으로 작업을 조회하고 상태를 갱신할 수 있는 기반을 만듭니다.

## 핵심 결정

### Spring API 호출 기반

초기 MVP에서는 queue 없이 Spring 서버가 Python AI 서버의 분석 실행 API를 직접 호출합니다.

```text
Spring API
  -> POST /api/v1/analysis-jobs/{analysis_job_id}/run
  -> Python AI server
  -> analysis_job_id 기준 DB 조회
  -> status/current_step/error 갱신
```

작업 유실, 재시도, 동시성 제어, 장시간 처리 문제가 확인되면 SQS 같은 queue 기반 구조로 전환합니다.

### 사용자 권한

Python AI 서버는 사용자 인증과 작품 소유권 검증을 직접 수행하지 않습니다.

사용자 인증, 작품 소유권 검증, 사용자-facing API 응답은 Spring 서버가 담당합니다. Python AI 서버는 Spring 서버가 생성한 `analysis_jobs`를 신뢰하고, 작업 ID 기준으로 분석 처리와 상태 갱신만 담당합니다.

### 원문 저장

Python AI 서버는 원문 텍스트를 요청 body로 받지 않습니다.

회차 원문은 Spring 서버가 S3에 저장하고, Python AI 서버는 DB에서 S3 key를 조회해 원문을 읽습니다.

## 상태 모델

`AnalysisJobStatus`

| 상태 | 의미 |
| --- | --- |
| `PENDING` | 작업 생성 후 분석 대기 |
| `RUNNING` | Python worker 처리 중 |
| `SUCCEEDED` | 분석 성공 |
| `FAILED` | 분석 실패 |
| `CANCELED` | 사용자 또는 시스템 취소 |

`AnalysisStep`

| 단계 | 의미 |
| --- | --- |
| `LOADING` | 작업, 작품, 회차, 업로드 파일 메타데이터 조회 |
| `CHUNKING` | 원문 청킹 |
| `PREPROCESSING` | LLM 입력 전처리 |
| `EMBEDDING` | 임베딩 생성 |
| `SETTING_EXTRACTION` | 설정 후보 추출 |
| `VALIDATION` | LLM JSON 검증 |
| `PERSISTING` | 후보/근거 저장 |
| `DONE` | 처리 완료 |

## DB 접근 범위

첫 번째 이슈에서 우선 접근할 테이블:

| 테이블 | 사용 목적 |
| --- | --- |
| `analysis_jobs` | 분석 작업 조회와 상태 갱신 |
| `works` | 작품 메타데이터 조회 |
| `episodes` | 분석 대상 회차 조회 |
| `upload_batches` | 업로드 배치 기준 분석 대상 조회 |
| `upload_files` | 원본 파일과 회차 생성 범위 조회 |

후속 이슈에서 추가 접근할 테이블:

| 테이블 | 사용 목적 |
| --- | --- |
| `episode_chunks` | 청크 저장, 원문 근거, pgvector 검색 |
| `setting_candidates` | AI 추출 후보 저장 |
| `characters` | 캐릭터 upsert 후보/확정 설정 조회 |
| `character_facts` | 검토 후 설정 이력 관리 |

## API

### 분석 작업 실행

```http
POST /api/v1/analysis-jobs/{analysis_job_id}/run
```

Request

```json
{
  "force": false
}
```

Response

```json
{
  "analysis_job_id": "01970c2e-7e6d-7000-8e5d-2a9bc4b6d333",
  "status": "RUNNING",
  "current_step": "LOADING",
  "message": "Analysis job accepted. Worker persistence is not wired yet."
}
```

현재 구현은 DB persistence가 연결되지 않은 초안입니다. DB 접근 작업이 추가되면 이 API는 작업을 조회하고 `RUNNING`으로 갱신한 뒤 worker를 실행합니다.

### 분석 작업 상태 조회

```http
GET /api/v1/analysis-jobs/{analysis_job_id}/status
```

현재 구현은 임시 `PENDING` 응답을 반환합니다. DB 접근 작업이 추가되면 `analysis_jobs` 테이블의 상태를 조회합니다.

## Worker Workflow

1. API route가 `analysis_job_id`와 실행 옵션을 받습니다.
2. Service가 worker 실행을 조율합니다.
3. Worker가 `analysis_job_id` 기준으로 DB에서 작업을 조회합니다.
4. 작업이 없으면 `ANALYSIS_JOB_NOT_FOUND`를 반환합니다.
5. 작업 상태를 `RUNNING`, `current_step=LOADING`으로 갱신합니다.
6. 원문 로드, 청킹, LLM 추출, 후보 저장은 후속 이슈에서 단계적으로 연결합니다.
7. 성공 시 `SUCCEEDED`, 실패 시 `FAILED`와 실패 사유를 저장합니다.

## 이후 작업

- SQLAlchemy DB session 구성
- `AnalysisJobRepository` 구현
- `analysis_jobs` 실제 조회/상태 갱신
- API 호출 시 background task 또는 worker execution 방식 결정
- 실패 사유 저장 필드와 retry 정책 확정
- Spring 서버의 분석 작업 생성 API와 호출 계약 맞추기
