# queue

Queue 기반 Worker 실행 흐름을 도입할 때 사용할 패키지입니다.

현재 MVP는 Spring 서버가 내부 Worker API를 제공하고, Python Worker가 claim API를 polling하는 방식으로 진행합니다. 따라서 이 패키지는 아직 실제 구현을 갖지 않는 placeholder입니다.

## 역할

후속 작업에서 queue 구조를 도입하면 다음 책임을 이 패키지에 둡니다.

- SQS 같은 queue에서 분석 작업 메시지를 소비합니다.
- queue message schema를 정의합니다.
- message type에 따라 적절한 Worker 실행 흐름으로 dispatch합니다.
- 실패 시 재시도, dead-letter queue, visibility timeout 같은 queue 운영 정책을 다룹니다.

## 현재 판단

멘토링 기준에 따라 초기에는 queue를 먼저 도입하지 않습니다.

먼저 Spring 내부 API claim/polling 방식으로 구현하고, 다음 문제가 실제로 확인되면 queue 도입을 다시 검토합니다.

- Worker가 많아질 때 polling 비용이 커지는 경우
- 분석 작업 지연이나 중복 실행 제어가 어려운 경우
- 실패 재시도와 DLQ 같은 운영 기능이 필요한 경우
- Spring API 직접 호출 방식으로 처리량 한계가 명확해지는 경우

## 예상 파일

- `sqs_consumer.py`: SQS 분석 job message consumer
- `messages.py`: queue message schema
- `dispatcher.py`: message type에 따른 Worker 실행 라우팅

## 현재 실행 방식

현재 Worker 실행 방식은 [AI Worker Workflow](../../docs/ai-worker-workflow.md)를 기준으로 확인합니다.
