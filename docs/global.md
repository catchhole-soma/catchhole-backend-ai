# Global Layer

## 목적

AI 서버의 전역 계층은 특정 분석 기능에 속하지 않는 공통 기반을 관리합니다.

현재 책임은 FastAPI 앱 생성, 라우터 등록, 환경변수 설정, 공통 예외 응답, Swagger 문서, S3 storage abstraction, 테스트 실행 기준입니다. 실제 분석 규칙은 `analysis`, `chunking`, `llm`, `embeddings`, `worker` 계층에 둡니다.

## 패키지 구조

```text
app
├── api
│   └── routes
├── core
├── domain
├── exceptions
├── schemas
├── services
├── storage
└── worker
```

향후 DB와 분석 기능이 붙으면 다음 패키지가 사용됩니다.

```text
app
├── analysis
├── chunking
├── db
├── embeddings
├── llm
├── models
├── queue
└── repositories
```

## FastAPI 계층 대응

FastAPI는 Spring처럼 강제되는 계층 구조가 없기 때문에, 유지보수를 위해 다음 기준으로 나눕니다.

| Python AI Server | Spring 기준 비유 | 책임 |
| --- | --- | --- |
| `api/routes` | Controller | HTTP 요청/응답 경계 |
| `schemas` | Request/Response DTO | API 입출력 검증과 Swagger schema |
| `services` | Service | 유스케이스 흐름 조율 |
| `worker` | Worker/Application job | 오래 걸리는 분석 작업 수행 |
| `repositories` | Repository | DB 접근 |
| `models` | Entity/ORM Model | DB 테이블 매핑 |
| `storage` | Storage adapter | S3 등 외부 저장소 접근 |
| `core` | Config | 전역 설정 |
| `exceptions` | Exception handler | 공통 예외 응답 |

## 공통 예외 응답

Python AI 서버는 내부 실패를 Spring 서버가 해석하기 쉽도록 공통 실패 응답을 사용합니다.

```json
{
  "success": false,
  "message": "요청 값이 올바르지 않습니다.",
  "error": {
    "code": "INVALID_REQUEST",
    "detail": {}
  },
  "timestamp": "2026-06-18T00:00:00+00:00"
}
```

| 필드 | 설명 |
| --- | --- |
| `success` | 실패 응답에서는 `false` |
| `message` | 기본 에러 메시지 |
| `error.code` | Python AI 서버 내부 에러 코드 |
| `error.detail` | 디버깅과 Spring 서버 로깅에 필요한 상세 정보 |
| `timestamp` | 응답 생성 시각 |

성공 응답은 우선 FastAPI `response_model`을 사용합니다. 사용자-facing 공통 응답 envelope는 Spring 서버에서 정리합니다.

## 예외 처리

비즈니스 예외는 `AppException`에 `ErrorCode`를 담아 던집니다.

`register_exception_handlers()` 처리 흐름

| 예외 | 응답 코드 |
| --- | --- |
| `AppException` | `ErrorCode`에 매핑된 HTTP status |
| `RequestValidationError` | `INVALID_REQUEST` |
| 기타 `Exception` | `INTERNAL_SERVER_ERROR` |

예상하지 못한 예외는 내부 exception message를 그대로 노출하지 않고 공통 서버 오류 메시지로 응답합니다.

## 설정 파일과 환경변수

`.env.example`을 기준으로 로컬 `.env`를 구성합니다.

| 설정 | 설명 |
| --- | --- |
| `APP_NAME` | FastAPI 앱 이름 |
| `APP_VERSION` | 앱 버전 |
| `APP_ENV` | 실행 환경 |
| `DATABASE_URL` | Spring 서버와 공유하는 PostgreSQL 연결 문자열 |
| `AWS_REGION` | S3 region |
| `AWS_S3_BUCKET` | 회차 원문과 업로드 파일이 저장되는 S3 bucket |
| `AWS_SQS_QUEUE_URL` | 추후 queue 전환 시 사용할 SQS URL |
| `LLM_API_KEY` | LLM API key |

## Swagger

FastAPI는 기본 Swagger 문서를 제공합니다.

| 문서 | 경로 |
| --- | --- |
| Swagger UI | `/docs` |
| ReDoc | `/redoc` |
| OpenAPI JSON | `/openapi.json` |

로컬 서버 기준:

```text
http://localhost:8000/docs
http://localhost:8000/redoc
```

## Storage

스토리지 접근은 `storage` 패키지에서 관리합니다.

현재 `S3TextObjectStorage`는 S3 object key를 기준으로 UTF-8 텍스트를 읽는 최소 기능만 제공합니다.

```text
S3 key
  -> boto3 get_object
  -> Body bytes
  -> UTF-8 text
```

Spring 서버가 업로드와 사용자 소유권 검증을 담당하고, Python AI 서버는 DB에 저장된 S3 key를 사용해 원문을 읽습니다.

## 테스트

로컬 테스트:

```bash
source .venv/bin/activate
pytest
```

현재 테스트 범위는 health check API입니다.

## 이후 작업

- DB session과 repository가 추가되면 `db`, `models`, `repositories` 기준을 이 문서에 반영합니다.
- Python AI 서버를 외부에서 직접 호출하게 되면 internal API key 또는 네트워크 제한 정책을 문서화합니다.
- S3 write/delete가 Python 서버 책임으로 확장되면 storage 문서를 분리합니다.
- Queue 전환 시 `queue` 패키지와 message schema를 별도 문서로 분리합니다.
