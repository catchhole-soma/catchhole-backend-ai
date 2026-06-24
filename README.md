# catchhole-backend-ai

CatchHole의 원고 분석 기능을 담당하는 Python AI 서버입니다.

프로젝트 전체 ERD와 분석 workflow는 Spring 백엔드 저장소의 `docs/`에서 관리합니다.
이 저장소에서는 Python 서버 실행 방법과 패키지별 책임만 정리합니다.

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

## 패키지 문서

각 패키지의 세부 책임은 패키지 내부 README에서 관리합니다.

- `app/analysis/README.md`: 설정 추출, 근거 위치 계산, 충돌 검사
- `app/chunking/README.md`: 원문 정규화, 문단 분리, 청킹, offset 기준
- `app/db/README.md`: DB session과 트랜잭션 경계
- `app/embeddings/README.md`: 임베딩 대상 선정과 RAG 검색
- `app/llm/README.md`: LLM client, prompt, 구조화 응답
- `app/mappers/README.md`: 계층 간 객체 변환
- `app/models/README.md`: SQLAlchemy ORM 모델
- `app/repositories/README.md`: DB 조회와 저장 계층
- `app/services/README.md`: 유스케이스 흐름 조율
- `app/storage/README.md`: S3 같은 외부 object storage 접근
- `app/queue/README.md`: queue consumer를 도입할 때의 책임

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
