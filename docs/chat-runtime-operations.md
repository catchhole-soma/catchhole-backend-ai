# 챗봇 배포와 검색 백필 운영 (#258)

## 배포와 데이터 준비

Java PR을 먼저 배포한 뒤 AI PR을 배포한다. Python 이미지 발행 성공과 최신 main SHA 확인,
최신 Java 배포 성공·health 확인은 기존 workflow가 유지한다. 같은 SHA의 파일을
`/opt/catchhole/releases/<full-sha>/`에 내려받고, 환경·내부 API 인증·검색 DB를 검사한 뒤
기존 5+1+1 워커와 별도 챗봇을 갱신한다. 프로세스 수는 실제 Compose 설정에서 읽는다.
검사 실패 시 기존 서비스는 변경하지 않는다. 서비스 재생성 이후 실패의 자동 롤백은 제공하지 않는다.

정상 기동 후 `current-chat-release` 링크를 원자적으로 바꾸고 systemd timer를 설치한다.
`catchhole-retrieval-backfill.service`는 5분마다 최대 300초의 one-shot을 실행한다.
실행 중이면 중복 기동하지 않으며, 재배포 중 실행 중인 백필은 시작한 이미지로 마친다.
다음 실행부터 새 release를 사용한다. 백필에 HTTP 포트는 없다. 서버 배포 성공은 전체 백필 완료를 뜻하지 않는다.
각 실행은 결과를 journald에 남기며 `status`가 영속 진행 상황을 조회한다.

## 최초 한 번 필요한 설정

API 서버의 `api.env`에 `CHAT_OPERATIONS_ENABLED=true`, 별도 32자 이상
`CHAT_CAPABILITY_SECRET`을 설정한다. 비밀값은 내부 API key와 다르게 생성하고 재배포마다 바꾸지 않는다.
백필 준비 중 `CHAT_ENABLED=false`를 유지할 수 있다. 사용자 공개 때
`CHAT_PYTHON_URL=http://<AI-private-IP>:8082`와 `CHAT_ENABLED=true`를 적용한다.
환경 파일 변경은 컨테이너 재생성이 필요하다. 기존 예약의 보고·복구를 위해 secret을 보존한다.

AI 첫 배포는 `chatbot.env`, `retrieval.env`가 없으면 0600 예제를 생성하고 미설정 항목에서 중단한다.
환경 파일은 커밋하지 않는다. 실제 값은 `/opt/catchhole/`에 한 번 넣고 같은 Actions 배포를 다시 실행한다.
기존 파일은 자동 배포가 덮어쓰지 않는다. 공통 자격 증명은 `worker.env`를 먼저 읽어 상속하며,
그 뒤 서비스별 파일의 값이 덮어쓴다. 이미지 SHA만 배포 인자로 고정한다.

| 파일 | 필수 설정 |
|---|---|
| `worker.env` | 기존 DB/S3·Spring 내부 주소/key·provider key. 공통 설정은 재복사하지 않는다 |
| `chatbot.env` | `CHAT_QA_ENABLED=true`, `CHAT_BIND_ADDRESS=<AI-private-IP>`; 모델 기본 `gpt-6-luna` |
| `chatbot.env` 원문 검색 | `CHAT_RETRIEVAL_ENABLED=true`, `CHAT_RETRIEVAL_DATABASE_URL=<SELECT-only DSN>`; 버전 `v1` |
| `retrieval.env` | `RETRIEVAL_BACKFILL_ENABLED=true`, `RETRIEVAL_EMBEDDING_GENERATION_ENABLED=true`, 안정적인 `RETRIEVAL_BACKFILL_ID`, 전체 `RETRIEVAL_BACKFILL_MAX_INPUT_TOKENS`, `RETRIEVAL_BACKFILL_ACTOR` |
| `retrieval.env` 범위 | `RETRIEVAL_BACKFILL_SCOPE=active-works` 또는 scope를 비우고 `RETRIEVAL_BACKFILL_WORK_IDS=<UUID,...>` 중 하나 |
| `retrieval.env` 자원 | 실제 API pool을 `AI_RUNTIME_API_DB_POOL_SIZE`에, 확인한 앱 전체 연결 예산을 `AI_RUNTIME_DB_CONNECTION_BUDGET`에 지정 |

AI SG의 TCP 8082는 API SG에서만 허용한다. API TCP 8080은 AI SG에서 접근 가능해야 한다.
RDS 접근과 S3 IAM 권한은 기존 정책을 사용한다. 검색 계정에는 CONNECT/USAGE 및
`retrieval_chunks`, `retrieval_sources`, `retrieval_current_chunks`, `episodes`, `works`, `members`,
`episode_source_purge_requests` SELECT가 필요하다. view 권한만으로는 실제 검색 쿼리가 실행되지 않는다.
검색 DSN은 `postgresql+psycopg` 형식이며 비밀번호 URL 인코딩과 TLS 설정을 유지한다.

기본 연결은 API 10 + 분석 5×3 + 비교 2 + 챗봇 2 + 백필 2 = 31이다.
배포 사전 검사 연결 1개까지 **32개**이며 RDS 관리·다른 앱 연결 여유는 별도다.
기존 27개에서 분석 워커 1개만 줄이면 충분하다는 예전 계산은 챗봇 연결을 빠뜨렸다.
실제 RDS `max_connections`와 기존 연결, CPU·메모리를 확인하고 예산을 설정한다.
이번 배포는 기존 워커 수나 DB pool을 자동으로 줄이거나 늘리지 않는다.

## 백필 범위·예산·중단 재개

campaign ID는 배포 SHA/날짜로 생성하지 않는다. 같은 ID의 작품 범위·모델·버전·전체 예산·운영 주체는
영속 SQLite에 고정된다. 변경해서 재사용하면 실패한다. 새 ID는 새로운 예산이므로 의도적인 새 작업에만 쓴다.
사용자 토큰은 차감하지 않으며 기존 서비스 부담 원장으로 기록한다.

한 순회 시작 시 대상 작품·회차 번호 상한·생성 시점을 고정한다. `(episode_no, id)`로 순회하므로
페이지 제한이나 같은 회차 번호에서도 건너뛰지 않는다. 회차의 모든 현재 청크가 READY인 경우만 전진한다.
시간/청크 제한은 같은 위치에서 다음 실행으로 이어지고, 완료된 청크는 재호출하지 않는다.
한 순회가 끝나면 다음 정기 실행이 새 원고·수정 원고를 탐색한다. 과거 버전·설정집은 지원 범위가 아니다.

provider 호출 전에 보수적인 UTF-8 입력 상한을 SQLite에 기록한다. 이 누적 상한은 실측 provider 토큰이
아니며 재시작/알려진 실패에도 환급하지 않는다. 기존 사용량 원장은 별도로 실제 측정값을 유지한다.
예산 소진이면 새 호출을 멈춘다. 프로세스 종료나 응답·저장 사이 실패처럼 호출 결과가 불확실하면
`BLOCKED / UNKNOWN_USAGE`로 멈추며 자동 유료 재실행하지 않는다. 정상 보고는 기존 outbox에서 먼저 복구한다.
`resume`은 운영자가 원장·저장 상태를 확인한 뒤 명시적으로 재개할 때만 사용하며 누적 예산은 초기화하지 않는다.
외부 API와 PostgreSQL의 분산 원자성을 제공하지 않으므로 명시적 재개에도 중복 호출 가능성은 남는다.

영속 `retrieval_outbox` 볼륨에는 기존 `retrieval.sqlite` 보고와 새 `backfill.sqlite3` 상태가 있다.
완료 후에도 상태·예산을 삭제하지 않는다. 이 파일은 비공개 ID를 포함할 수 있으므로 외부에 공유하지 않는다.
SQLite에는 원문이나 키를 저장하지 않으며 로그는 상태·개수·예산만 출력한다.

## 확인·복구 명령

AI 서버에서 `sudo -iu ubuntu` 후 실행한다.

```bash
cd /opt/catchhole
python3 current-chat-release/manage-ai-runtime.py status
systemctl status catchhole-retrieval-backfill.timer
systemctl status catchhole-retrieval-backfill.service
journalctl -u catchhole-retrieval-backfill.service -n 30 --no-pager
```

`READY`는 해당 순회에서 확인한 지원 원문 범위가 준비됐다는 뜻이다. 이후 수정·추가된 원고와
검색 품질을 보증하지 않는다. `PARTIAL`은 제외/확인 불가 출처, `REPORTS_PENDING`은 원장 보고 대기,
`BLOCKED`는 명시적인 확인 필요 상태다. `BUDGET_EXHAUSTED`는 전체 상한 소진이며 자동 재충전하지 않는다.
`TIME_LIMIT`/`CHUNK_LIMIT`은 다음 정기 실행에서 이어간다. `status`가 실행과 겹치면 `INDEX_BUSY`가 반환될 수 있다.

```bash
# 원장·DB 확인 후에만: 불확실 상태 해제, 예산 환급 없음
python3 current-chat-release/manage-ai-runtime.py resume
# 다음 주기를 기다리지 않고 실행
sudo systemctl start --no-block catchhole-retrieval-backfill.service
```

새 유료 작업 중단은 `retrieval.env`의 `RETRIEVAL_BACKFILL_ENABLED=false`와 API의
`CHAT_OPERATIONS_ENABLED=false`로 제어한다. 이미 외부로 전송된 호출은 취소되지 않는다.
timer를 유지하면 disabled 상태에서도 outbox 보고 복구가 계속된다. timer만 끄면 보고 복구도 멈춘다.
사용자 공개 중단은 별도로 `CHAT_ENABLED=false`를 적용한다. 원장/SQLite 볼륨은 삭제하지 않는다.

## 검증 경계

preflight는 유료 호출·사용자 원문 읽기 없이 설정, 내부 인증, Spring health, DB 스키마·SELECT 권한을 확인한다.
provider key의 실제 유효성·모델 접근 권한, 실제 S3 GetObject 권한, 답변 품질·검색 완료까지 보증하지 않는다.
실제 PostgreSQL의 정상 역할/기본 테이블 권한 누락/뷰 권한 누락/벡터 차원 오류를 격리 환경에서 검증했다.
운영 활성화·유료 백필은 이 PR 작성 중 실행하지 않았다. 새 배포 후 RDB 질문·원문 질문과 근거를 확인한다.
