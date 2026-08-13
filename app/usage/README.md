# usage

AI provider 호출 전후의 토큰 예약·정산을 공통으로 감싸는 패키지입니다.

## 역할

- LLM·임베딩 요청마다 고유 `request_id`를 생성합니다.
- 모델 tokenizer로 센 입력량에 10%와 256 token의 안전 여유, 최대 출력량을 더해 Spring에 먼저 예약합니다.
- tokenizer를 사용할 수 없는 모델이나 환경에서는 기존 UTF-8 byte 상한으로 안전하게 되돌아갑니다.
- provider가 반환한 input/cached input/output usage를 Spring 원장에 정산합니다.
- provider 사용량을 알 수 없는 실패는 예약을 전액 해제합니다.
- 캐릭터·세계관 추출 재시도, subject fallback, 세계관 대상 탐색·비교, batch embedding을 서로 다른 `purpose`로 기록합니다.
- reserve에는 현재 claim의 lease token을 보내 중복 Worker가 새 provider 요청을 시작하지 못하게 합니다.

prompt나 응답 본문은 Spring에 보내지 않습니다. 이 패키지는 기존 OpenAI client를 delegate로 감싸므로 추출·임베딩 계층은 계량 HTTP 계약을 알 필요가 없습니다.

## 호출 결과

| 상황 | 원장 처리 |
| --- | --- |
| provider 성공 | 실제 usage로 `SETTLED/SUCCESS` |
| HTTP 실패 응답에 usage 존재 | 실제 usage로 `SETTLED/FAILURE` |
| provider 호출 전 실패 또는 usage 확인 불가 | `RELEASED/USAGE_UNAVAILABLE` |
| Spring reserve에서 한도 초과 | provider를 호출하지 않고 분석 실패 전파 |
| Worker lease 만료 | Backend가 남은 `RESERVED` 요청을 `WORKER_LEASE_EXPIRED`로 해제 |

cached input은 전체 input에 이미 포함되므로 관측값으로만 전달하고 별도 가산하지 않습니다.
배포 이미지는 GPT-5.6 계열이 사용하는 `o200k_base` tokenizer를 빌드 시점에 미리 저장해 런타임 네트워크에 의존하지 않습니다.

현재 purpose는 `SETTING_EXTRACTION`, `SUBJECT_RESOLUTION`, `CHUNK_EMBEDDING`, `CHARACTER_FACT_COMPARISON`, `WORLD_SETTING_EXTRACTION`, `WORLD_SETTING_SUBJECT_RESOLUTION`, `WORLD_SETTING_COMPARISON`입니다. 정산·해제는 provider 응답이 lease 만료 뒤 도착해도 기존 request ID의 예약을 닫을 수 있도록 lease 없이 같은 request ID로 멱등 처리합니다.

실제 분석에서 확인한 예약량과 Prompt Cache 적중 조건은
[`docs/ai-token-cache-validation.md`](../../docs/ai-token-cache-validation.md)에 기록합니다.
