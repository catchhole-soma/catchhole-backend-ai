# usage

AI provider 호출 전후의 토큰 예약·정산을 공통으로 감싸는 패키지입니다.

## 역할

- LLM·임베딩 요청마다 고유 `request_id`를 생성합니다.
- 모델 tokenizer로 센 입력량에 10%와 256 token의 안전 여유, 최대 출력량을 더해 Spring에 먼저 예약합니다.
- tokenizer를 사용할 수 없는 모델이나 환경에서는 기존 UTF-8 byte 상한으로 안전하게 되돌아갑니다.
- provider가 반환한 input/cached input/output usage를 Spring 원장에 정산합니다.
- provider 사용량을 알 수 없는 실패는 예약을 전액 해제합니다.
- 설정 추출 재시도, subject fallback, batch embedding을 서로 다른 `purpose`로 기록합니다.

prompt나 응답 본문은 Spring에 보내지 않습니다. 이 패키지는 기존 OpenAI client를 delegate로 감싸므로 추출·임베딩 계층은 계량 HTTP 계약을 알 필요가 없습니다.

## 호출 결과

| 상황 | 원장 처리 |
| --- | --- |
| provider 성공 | 실제 usage로 `SETTLED/SUCCESS` |
| HTTP 실패 응답에 usage 존재 | 실제 usage로 `SETTLED/FAILURE` |
| provider 호출 전 실패 또는 usage 확인 불가 | `RELEASED/USAGE_UNAVAILABLE` |
| Spring reserve에서 한도 초과 | provider를 호출하지 않고 분석 실패 전파 |

cached input은 전체 input에 이미 포함되므로 관측값으로만 전달하고 별도 가산하지 않습니다.
배포 이미지는 `gpt-4.1-mini` tokenizer를 빌드 시점에 미리 저장해 런타임 네트워크에 의존하지 않습니다.
