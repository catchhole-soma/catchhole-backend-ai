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
- `LLM_MODEL`: 설정 추출에 사용할 LLM 모델명
- `OPENAI_RESPONSES_API_URL`: OpenAI Responses API endpoint
- `SPRING_INTERNAL_API_BASE_URL`: Spring 내부 Worker API base URL. 기본값 `http://localhost:8080`은 로컬 개발용 값입니다.
- `SPRING_INTERNAL_API_KEY`: Spring 내부 Worker API 호출에 사용할 `X-Internal-Api-Key` 값

## API 초안

- `GET /api/v1/health`
- `GET /api/v1/analysis-jobs/{analysis_job_id}/status`

분석 실행은 Python API가 `analysis_job_id`를 직접 받는 방식이 아니라, Python Worker가 Spring 내부 Worker API의 claim endpoint를 호출해 가져오는 방식으로 진행합니다.

## 패키지 문서

각 패키지의 세부 책임은 패키지 내부 README에서 관리합니다.

- `app/analysis/README.md`: 설정 추출, 근거 위치 계산, 충돌 검사
- `app/chunking/README.md`: 원문 정규화, 문단 분리, 청킹, offset 기준
- `app/clients/README.md`: Spring 내부 API 같은 외부 HTTP client
- `app/db/README.md`: DB session과 트랜잭션 경계
- `app/embeddings/README.md`: 임베딩 대상 선정과 RAG 검색
- `app/llm/README.md`: LLM client, prompt, 구조화 응답
- `app/mappers/README.md`: 계층 간 객체 변환
- `app/models/README.md`: SQLAlchemy ORM 모델
- `app/repositories/README.md`: DB 조회와 저장 계층
- `app/services/README.md`: 유스케이스 흐름 조율
- `app/storage/README.md`: S3 같은 외부 object storage 접근
- `app/worker/README.md`: Spring claim 기반 Worker 실행 흐름과 상태/단계 정책
- `app/queue/README.md`: queue consumer를 도입할 때의 책임

### schema와 dataclass 사용 기준

외부 API 경계와 내부 계산용 값을 구분하기 위해 다음 기준을 사용합니다.

- Pydantic `BaseModel`
  - FastAPI Request/Response에 사용합니다.
  - Spring 내부 API payload처럼 외부 경계에서 JSON 직렬화/검증이 필요한 값에 사용합니다.
  - LLM 응답 JSON처럼 외부에서 들어온 불안정한 구조를 검증해야 하는 경우에도 사용합니다.
  - API 계약용 schema 위치: `app/schemas`
  - 특정 도메인 내부에서만 쓰는 검증 schema는 해당 패키지 안에 둘 수 있습니다.
    - 예: `app/analysis/schemas.py`
- `dataclass`
  - API로 직접 노출되지 않는 내부 알고리즘 결과에 사용합니다.
  - 이미 검증된 값을 내부에서 전달하는 가볍고 불변에 가까운 값 객체에 사용합니다.
  - 예: `Paragraph`, `EpisodeChunkDraft`, `LlmTextResponse`
- SQLAlchemy model
  - DB 테이블과 직접 매핑되는 영속 객체에 사용합니다.
  - 위치: `app/models`

정리하면, API 입출력과 외부 JSON 검증 경계는 `BaseModel`, 내부 순수 로직의 중간 결과는 `dataclass`, DB 테이블 매핑은 `models(SQLAlchemy)`를 기본으로 합니다.

### Python 네이밍/메서드 컨벤션

Python에서는 이름 앞뒤의 `_` 개수에 따라 의미가 달라질 수 있습니다.

- `_name`
  - 내부 구현용 이름이라는 관례입니다.
  - Java의 `private`처럼 접근을 강제로 막지는 않지만, 외부 계층에서 직접 호출하지 않는 것을 의미합니다.
  - 예: `_headers()`, `_url()`, `_run_analysis_steps()`
- `__name`
  - name mangling이 적용됩니다.
  - 클래스 내부 구현을 하위 클래스에서 실수로 덮어쓰는 것을 줄이고 싶을 때 사용합니다.
  - 일반적인 private 용도로는 잘 사용하지 않습니다.
- `__name__`
  - Python이 특별한 의미로 사용하는 magic method 또는 dunder 이름입니다.
  - 직접 임의로 만들기보다 Python 표준 프로토콜에서 정한 이름을 사용합니다.
  - 예: `__init__`, `__enter__`, `__exit__`

이 프로젝트에서는 일반적인 내부 helper 메서드는 `_name` 형식을 사용합니다.
외부 계층에서 호출해야 하는 공개 메서드는 `_` 없이 작성합니다.

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
