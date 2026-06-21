# catchhole-backend-ai

CatchHole의 원고 분석, 청킹, 설정 후보 추출, 근거 저장 흐름을 담당하는 Python AI 서버입니다.

## 역할

- Spring 서버가 생성한 `analysis_jobs`를 기준으로 분석 작업을 수행합니다.
- S3에 저장된 회차 원문을 읽어 회차/청크 단위로 분리합니다.
- AI가 추출한 설정 후보와 근거 정보를 PostgreSQL에 저장할 예정입니다.
- Spring 서버는 사용자 인증, 작품 소유권 검증, API 응답, 사용자의 후보 확정 흐름을 담당합니다.

## 현재 범위

아직 실제 DB/S3/LLM 연동은 붙이지 않았고, 서버 구조와 분석 잡 실행 계약을 먼저 잡았습니다.

```text
Spring API
  -> 사용자 인증과 작품 소유권 검증
  -> episodes / upload_files / analysis_jobs 생성
  -> Python AI 서버 분석 API 호출
  -> Python worker가 S3 원문 조회
  -> chunk / setting_candidates / evidence 저장
  -> Spring API가 진행률과 결과를 조회
```

초기 MVP에서는 Spring 서버가 Python AI 서버의 분석 실행 API를 직접 호출합니다.
작업 유실, 재시도, 동시성 제어, 장시간 처리 문제가 확인되면 SQS 같은 queue 기반 구조로 전환합니다.

## 미구현 범위

- PostgreSQL session / repository 실제 연결
- S3 원문 조회와 worker 분석 흐름 연결
- 회차 분리, 청킹, source offset 계산
- LLM 호출과 설정 후보 JSON 검증
- embedding 생성과 RAG 검색
- queue consumer
- internal API 인증

## 로컬 실행

Python 3.12 이상을 사용합니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

Mac에서 Anaconda Python을 사용할 경우:

```bash
/opt/anaconda3/bin/python3.12 -m venv .venv
```

헬스 체크:

```bash
curl http://localhost:8000/api/v1/health
```

Swagger 문서:

```text
http://localhost:8000/docs
http://localhost:8000/redoc
```

테스트:

```bash
pytest
```

## 환경 변수

`.env.example`을 참고해 `.env`를 생성합니다.

- `DATABASE_URL`: Spring 서버와 공유하는 PostgreSQL 연결 문자열
- `AWS_S3_BUCKET`: 회차 원문과 업로드 파일이 저장되는 S3 버킷
- `AWS_SQS_QUEUE_URL`: 분석 잡 큐를 붙일 경우 사용할 SQS URL
- `LLM_API_KEY`: 설정 추출/검증에 사용할 LLM API 키

## API 초안

- `GET /api/v1/health`
- `POST /api/v1/analysis-jobs/{analysis_job_id}/run`
- `GET /api/v1/analysis-jobs/{analysis_job_id}/status`

## 코드 구조 규칙

FastAPI는 Spring처럼 계층 구조를 강제하지 않으므로, 프로젝트 안에서 다음 기준으로 역할을 나눕니다.

- `app/api/routes`: 외부 요청을 받는 API 라우터입니다. Spring의 Controller에 가깝습니다.
- `app/services`: 유스케이스 흐름을 조율합니다. Spring의 Service에 가깝습니다.
- `app/worker`: 실제 분석 실행 흐름을 담당합니다. S3 조회, 청킹, LLM 호출 같은 작업이 연결됩니다.
- `app/repositories`: DB 조회와 저장을 담당합니다. Spring의 Repository에 가깝습니다.
- `app/models`: SQLAlchemy ORM 모델입니다. DB 테이블과 매핑됩니다.
- `app/schemas`: FastAPI 요청/응답 모델입니다. Pydantic `BaseModel`을 사용합니다.
- `app/mappers`: ORM 모델을 응답 schema로 변환합니다.

### schema와 dataclass 사용 기준

외부 API 경계와 내부 계산용 값을 구분하기 위해 다음 기준을 사용합니다.

- Pydantic `BaseModel`
  - FastAPI Request/Response에 사용합니다.
  - Swagger 문서에 노출되거나 JSON 직렬화/검증이 필요한 값에 사용합니다.
  - 위치: `app/schemas`
- `dataclass`
  - API로 직접 노출되지 않는 내부 알고리즘 결과에 사용합니다.
  - 청킹 중간 결과, offset 계산 결과처럼 가볍고 불변에 가까운 값 객체에 사용합니다.
  - 예: `Paragraph`, `EpisodeChunkDraft`
- SQLAlchemy model
  - DB 테이블과 직접 매핑되는 영속 객체에 사용합니다.
  - 위치: `app/models`

정리하면, API 입출력은 `schemas(BaseModel)`, 내부 순수 로직의 중간 결과는 `dataclass`, DB 테이블 매핑은 `models(SQLAlchemy)`를 기본으로 합니다.

## 예외 응답

Python AI 서버의 실패 응답은 Spring 서버가 해석하기 쉽도록 공통 형태를 사용합니다.

```json
{
  "success": false,
  "message": "요청 값이 올바르지 않습니다.",
  "error": {
    "code": "INVALID_REQUEST",
    "detail": {}
  },
  "timestamp": "2026-06-13T00:00:00+00:00"
}
```

성공 응답은 FastAPI response model을 우선 사용하고, Spring API의 사용자-facing 응답은 Spring 서버에서 정리합니다.
