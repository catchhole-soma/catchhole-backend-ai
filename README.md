# catchhole-backend-ai

CatchHole의 원고 분석 기능을 담당하는 Python AI 서버입니다.

프로젝트 전체 ERD와 분석 workflow는 Spring 백엔드 저장소의 `docs/`에서 관리합니다.
이 저장소에서는 Python 서버 실행 방법과 패키지별 책임만 정리합니다.

## 문서 기준

문서 책임은 다음 기준으로 나눕니다.

- 프로젝트 전체 ERD, 사용자-facing API, 도메인 정책은 Spring 백엔드 저장소의 `docs/`에서 관리합니다.
- Python repo의 `docs/`는 Python Worker 내부 실행 흐름처럼 여러 Python 패키지를 가로지르는 내용을 정리합니다.
- 특정 패키지 내부 책임은 각 `app/*/README.md`에 정리합니다.

자세한 기준은 [docs/README.md](docs/README.md)를 확인합니다.
현재 Worker 전체 처리 흐름은 [docs/ai-worker-workflow.md](docs/ai-worker-workflow.md)를 기준으로 읽습니다.
FastAPI 유지/축소 여부는 [docs/fastapi-role-review.md](docs/fastapi-role-review.md)에 검토 기준을 남깁니다.

## 로컬 실행

Python 3.12 이상을 사용합니다.

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

위 FastAPI 실행은 health check와 임시 HTTP route 확인용입니다.
실제 분석 실행은 Worker runner를 기준으로 합니다.

```bash
.venv/bin/python scripts/run_analysis_worker.py
```

S3/DB/Spring 연결 없이 로컬 텍스트 파일 하나로 청킹, LLM 설정 후보 추출, 근거 위치 보정을
확인하려면 다음 runner를 사용합니다.

```bash
.venv/bin/python scripts/run_episode_text_analysis_debug.py \
  --text-file ./samples/episode-1.txt \
  --episode-no 1 \
  --episode-title "1화" \
  --max-chunks 1 \
  --output-json ./tmp/episode-1-debug.json
```

`episodeId`, `workId`, `analysisJobId`는 넘기지 않으면 실행할 때마다 가상 UUID를 생성합니다.
이 runner는 `episode_chunks`나 `setting_candidates`에 저장하지 않고, 결과를 콘솔과 JSON 파일로만 출력합니다.

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

## Docker 실행

컨테이너 이미지는 기본적으로 Spring 내부 API에서 분석 작업을 claim하는 Worker를 실행합니다.

```bash
docker build -t catchhole-ai:local .
docker run --rm --env-file .env catchhole-ai:local
```

FastAPI 서버를 확인해야 할 때는 command를 override합니다.

```bash
docker run --rm -p 8000:8000 --env-file .env catchhole-ai:local \
  uvicorn app.main:app --host 0.0.0.0 --port 8000
```

`main` 브랜치에 push되면 GitHub Actions가 GHCR에 `ghcr.io/catchhole-soma/catchhole-backend-ai:main`과 short SHA 태그를 발행합니다.
이미지 발행 후 백엔드 저장소의 EC2 배포 workflow를 호출하려면 AI 저장소의 Repository Secrets에 `BACKEND_DEPLOY_TOKEN`을 설정합니다.

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

## FastAPI API 초안

- `GET /api/v1/health`
- `GET /api/v1/analysis-jobs/{analysis_job_id}/status`

분석 실행은 Python API가 `analysis_job_id`를 직접 받는 방식이 아니라, Python Worker가 Spring 내부 Worker API의 claim endpoint를 호출해 가져오는 방식으로 진행합니다.
따라서 FastAPI route는 현재 분석 실행 경로가 아니라 보조 HTTP 인터페이스로 봅니다.

## 패키지 문서

각 패키지의 세부 책임은 패키지 내부 README에서 관리합니다.

- `app/analysis/README.md`: 설정 추출, 근거 위치 계산, 충돌 검사
- `app/chunking/README.md`: 원문 정규화, 문단 분리, 청킹, offset 기준
- `app/clients/README.md`: Spring 내부 API 같은 외부 HTTP client
- `app/db/README.md`: DB session과 트랜잭션 경계
- `app/embeddings/README.md`: 임베딩 대상 선정과 RAG 검색
- `app/exceptions/README.md`: FastAPI 공통 예외 응답과 ErrorCode 매핑
- `app/llm/README.md`: LLM client, prompt, 구조화 응답
- `app/mappers/README.md`: 계층 간 객체 변환
- `app/models/README.md`: SQLAlchemy ORM 모델
- `app/repositories/README.md`: DB 조회와 저장 계층
- `app/schemas/README.md`: FastAPI/Spring 내부 API JSON schema
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
