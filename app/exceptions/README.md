# exceptions

FastAPI 응답용 공통 예외와 에러 응답 변환 로직을 두는 패키지입니다.

Spring 기준으로는 `AppException`, `ErrorCode`, 전역 예외 handler 조합에 가깝습니다.

## 역할

- Python API에서 발생한 예외를 일정한 JSON 응답 형태로 변환합니다.
- 에러 코드와 HTTP status, 사용자 메시지를 한 곳에서 매핑합니다.
- FastAPI request validation 실패를 공통 실패 응답으로 변환합니다.

다음 책임은 Exceptions에 넣지 않습니다.

- Worker 내부 분석 실패 재시도 정책
- LLM 응답 검증 실패의 세부 처리
- Spring 내부 API에 실패 상태를 보고하는 로직
- 도메인별 복잡한 예외 복구 흐름

## 현재 파일

- `app_exception.py`
  - FastAPI route 또는 service에서 의도적으로 발생시키는 공통 예외입니다.
  - `ErrorCode`와 상세 정보를 함께 보관합니다.
- `error_code.py`
  - 에러 코드, HTTP status, 사용자 메시지를 매핑합니다.
- `handlers.py`
  - FastAPI app에 exception handler를 등록합니다.
  - `AppException`, `RequestValidationError`, 예상하지 못한 `Exception`을 공통 응답 형태로 바꿉니다.

## 응답 형태

Python API의 실패 응답은 Spring 서버가 해석하기 쉽도록 다음 형태를 사용합니다.

```json
{
  "success": false,
  "message": "요청 값이 올바르지 않습니다.",
  "error": {
    "code": "INVALID_REQUEST",
    "detail": {}
  },
  "timestamp": "2026-06-13T00:00:00"
}
```

## Analysis 내부 예외와의 구분

`app/analysis/exceptions.py`의 예외는 LLM 추출/검증 같은 분석 내부 흐름에서만 사용합니다.

예를 들어 `LlmExtractionError`는 사용자-facing API 응답을 만들기 위한 예외가 아니라, Worker가 분석 실패 사유를 구분하고 Spring에 보고하기 위한 내부 예외입니다.

정리하면 다음 기준을 따릅니다.

- FastAPI 응답으로 직접 변환될 예외: `app/exceptions`
- 특정 분석 로직 내부에서만 의미가 있는 예외: 해당 패키지 내부
