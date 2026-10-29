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
  - job type을 제한한 claim, lease heartbeat, progress, complete, fail API 호출을 담당합니다.
  - 분석 상태 API의 read timeout은 `SPRING_WORKER_READ_TIMEOUT_SECONDS`(기본 120초, 유한 양수)로 설정합니다. 최초 claim·후보 게시/비교·문맥·progress·complete·fail에 적용하며 연결·쓰기·pool 대기는 각각 30초를 유지합니다. 요청별로 적용하므로 동시에 실행하는 heartbeat와 token 원장은 기존 read 30초를 유지합니다.
  - read timeout은 응답 헤더/다음 바이트를 기다리는 한 번의 읽기 한도이며 요청 전체의 절대 시간 제한은 아닙니다. heartbeat의 60초 주기·최대 3회 시도·2/4초 재시도 간격과 lease 실패 시 소유 작업 취소는 유지합니다. 따라서 read 설정 전에 heartbeat 실패로 취소될 수 있습니다. 일반 분석 POST의 자동 재전송은 추가하지 않습니다.
  - claim 이후 상태·세계관·token 예약 요청에는 `X-Worker-Lease-Token`을 함께 전송합니다.
  - 세계관 후보 게시, 후보별 비교 claim, 대상명 페이지, 상세 문맥, 비교 완료/실패 API를 담당합니다.
  - Spring 공통 오류 envelope의 `error.code`와 허용된 `error.context.reasonCode`를 typed exception 속성으로 보존합니다. 응답 메시지나 원문 값은 분기 기준으로 파싱하지 않습니다.
  - 세계관 비교 실패 보고는 사용자용 상위 `comparisonFailureCode`와 Backend 원본 `sourceErrorCode`/`sourceReasonCode`를 별도 필드로 전달합니다.
  - 캐릭터 Fact 비교 후보 claim, 현재 snapshot 문맥 조회, 비교 완료/실패 API를 담당합니다. 네 endpoint는 모두 POST이며 Spring이 comparison lifecycle과 context stale 검증을 소유합니다.
  - AI provider 호출별 token reserve, settle, release 내부 API도 같은 공통 envelope 규칙으로 호출합니다. settle/release는 lease가 끝난 뒤에도 기존 예약을 정리할 수 있도록 request ID 기준으로 호출합니다.
  - token 원장 갱신은 일시적 네트워크 오류와 408/409/429/5xx에 같은 멱등 요청을 최대 3회 재시도합니다.
