# db

DB engine과 SQLAlchemy session 설정을 두는 패키지입니다.

Spring 기준으로는 JPA 설정과 `EntityManager` / transaction 기반 인프라 설정에 가깝습니다.

## 역할

- `DATABASE_URL`을 기준으로 SQLAlchemy engine을 생성합니다.
- request나 worker 흐름에서 사용할 session factory를 제공합니다.
- DB 연결 설정을 애플리케이션 코드와 분리합니다.

다음 책임은 DB 패키지에 넣지 않습니다.

- 도메인 로직
- 청킹/LLM 분석 흐름
- SQLAlchemy query 세부 구현
- 트랜잭션이 필요한 유스케이스 조율

## 현재 파일

- `session.py`
  - `DATABASE_URL`로 SQLAlchemy engine을 생성합니다.
  - cached session factory를 제공합니다.

## 트랜잭션 기준

Repository는 session을 사용해 조회/저장 대기 상태를 만들고, commit/rollback 경계는 Service 또는 Worker처럼 유스케이스를 조율하는 계층에서 관리합니다.

이 기준은 Spring에서 Repository가 저장소 접근을 담당하고, Service가 트랜잭션 경계를 갖는 구조와 비슷하게 이해하면 됩니다.

## 후속 검토

- 여러 Service에서 같은 commit/rollback 패턴이 반복되면 `transaction.py` 같은 helper를 추가할 수 있습니다.
- 다만 helper를 먼저 만들기보다, 실제 중복이 쌓인 뒤 도입합니다.
