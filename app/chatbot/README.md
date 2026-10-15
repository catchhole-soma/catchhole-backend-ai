# Chat execution runtime

`http_app`은 분석 Worker와 독립된 ASGI 진입점이며 기본 handler는 아직 미구현 상태를 반환한다. 전체 API·실행/원장 상태·배포 계약은 [chat-execution-v1](../../docs/chat-execution-v1.md)을 따른다.

- `client`: Spring이 소유한 실행·인증·토큰 API.
- `runtime`: 동시성/종료/deadline, 실행별 `tools()`와 `call()`.
- `providers`: 기존 TextGenerationClient/EmbeddingApi의 예약 추정과 usage 추출을 연결. 내부 자동 재시도 없음.
- `outbox`: 실행 결과와 실측 사용량 보고의 독립 재전송. provider 자체는 재생하지 않음.
- `http_app`: 내부 인증/크기 제한/health/metrics/프로세스 수명.

#238 handler는 `async def handler(execution: Execution) -> Finish` 규격을 사용한다. 해석/답변에는 목적별 ChatTextClient, 질의 embedding에는 ChatEmbeddingClient를 구성한다. 실제 저수준 client만 감싸고 분석용 Metered* wrapper를 중첩하지 않는다. 프롬프트 및 자연어 분기는 #238에 추가한다. provider 재시도·truncation 보정 호출은 새 callId와 예약을 받는다.

검증: `.venv/bin/pytest -q tests/test_chat_execution.py`. 실제 Spring 왕복 stub은 `tests/support/chat_stub_server.py`이며 운영 entry point에서 import하지 않는다.
