# clients

외부 HTTP API client를 두는 패키지입니다.

Spring 기준으로는 외부 시스템 adapter 또는 client 계층에 가깝습니다.

## 역할

- Python Worker가 호출해야 하는 외부 HTTP API를 감쌉니다.
- 인증 header, endpoint path, 공통 응답 envelope 파싱을 한 곳에 모읍니다.
- Service나 Worker가 HTTP 세부 구현을 직접 알지 않도록 분리합니다.

다음 책임은 Client에 넣지 않습니다.

- 청킹 실행
- LLM prompt 작성
- DB 저장
- 분석 상태 전이 판단

## 현재 파일

- `spring_worker_client.py`
  - Spring 내부 Worker API를 호출합니다.
  - base URL은 `SPRING_INTERNAL_API_BASE_URL` 환경변수로 주입하며, 기본값은 로컬 개발용 `http://localhost:8080`입니다.
  - `X-Internal-Api-Key` header를 사용합니다.
  - claim, progress, complete, fail API 호출을 담당합니다.
