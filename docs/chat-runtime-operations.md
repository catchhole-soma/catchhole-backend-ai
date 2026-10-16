# 챗봇 최초 설정·배포·검색 백필 (#258)

## 정상 배포 경로

Java #262와 공통 호스트 잠금을 추가한 Java #263을 먼저 배포한 뒤 AI #84를 머지한다.
기존 GitHub Actions 배포 자격 증명과 서버의 `api.env`·`worker.env`를 재사용한다.
새로운 키·DB 암호·사설 IP를 GitHub Secrets에 복사하거나 서버에서 SQL을 직접 실행할 필요가 없다.

1. 기존 workflow가 최신 Java main 배포 성공과 Spring health를 확인한다.
2. 같은 AI publish SHA의 최초 설정 코드를 내려받는다. 워커의 실제 사설 IP/VPC와 기존
   `SPRING_INTERNAL_API_BASE_URL`의 API 인스턴스를 대조한다. 서로 다른 VPC나 모호한 인스턴스는 거절한다.
3. 문서화된 API/Worker 보안 그룹이 실제 인스턴스에 연결됐는지 확인하고,
   bind 대상 primary ENI의 부착 SG 전체에서 8082 접근 규칙을 검사한다. API SG 외의 SG·CIDR·prefix list가
   8082를 허용하면 중단하고, 안전한 경우 필요한 API SG 전용 규칙을 추가한 뒤 재검증한다.
   포트 범위·전체 프로토콜 규칙도 포함하며 기존 규칙을 자동 삭제하지 않는다.
4. API 호스트에서 기존 비밀값을 보존하고 없는 실행 서명값만 생성한다. 초기 운영 백필 접수를 켜고
   AI 사설 주소를 연결한다. 공개 채팅은 기존에 열려 있었다면 유지하고, 첫 공개는 준비 완료까지 보류한다.
5. AI 호스트에서 공통 자격 증명을 상속해 서비스별 env를 준비한다. 기존 검색 DSN은 검사·재사용하고,
   없다면 읽기 전용 계정과 비밀번호를 생성해 운영 호스트에만 보관한다. 실제 DB 용량과 메모리를 확인한다.
6. 기존 readiness를 통과한 동일 SHA의 워커·챗봇을 배포하고, `current-chat-release`와 백필 timer를 설정한다.
7. 첫 공개가 남아 있다면 최대 30분 동안 백필 및 미전송 보고 상태를 확인한다.
   지원 원문 범위가 준비되고 AI의 health가 확인된 뒤 API의 공개 채팅을 활성화한다.

이 흐름은 **최초 설정을 자동 수행하는 배포**다. 실제 운영 적용·임베딩 공급자 호출은
PR 테스트와 별개이며, merge 이후 배포 단계에서 시작된다. 배포 성공과 색인 완료를 구분한다.

## 최초 자동 설정의 정책

기존 값이 없는 최초 rollout의 기본값은 다음과 같다. custom 모델·DSN·campaign·범위·예산은 보존한다.

| 항목 | 최초 정책 |
|---|---|
| 챗봇 | 독립 FastAPI, `gpt-6-luna`, QA 및 원문 검색 활성화 |
| 백필 대상 | `active-works`: 활성 작품의 현재 회차 원문, 이후 추가·수정분 포함 |
| campaign | `retrieval-v1-rollout`, 운영 주체 `scheduled-retrieval` |
| 누적 입력 상한 | `50,000,000` 보수적 입력 단위, 재배포/재시작으로 충전하지 않음 |
| 청크·모델 | 기존 800/100 토큰, `text-embedding-3-small`, 1536차원, v1 |
| 비용 귀속 | 기존 서비스 부담 원장 사용, 사용자 토큰 차감 없음 |
| 실행 | 5분 간격, 내부 작업 deadline 300초·1,000청크·batch 8·알려진 실패 재시도 1 |
| DB | 실제 Compose 풀 합계와 API pool을 사용. 기본 정상 31 + 점검 1 = 32 |

5천만은 공급자에게 반드시 소비하는 양이나 400회차의 측정 결과가 아니라 **유료 호출을 제한하는
누적 실행 상한**이다. 예산은 현재 원고와 이후 추가/수정분이 공유한다. 실제 provider 사용량은 별도 원장에 기록한다.
같은 campaign의 범위·모델·버전·예산·운영 주체는 최초 실행 후 바꾸지 않는다.
새 ID는 새 예산이므로 장애 복구 목적으로 자동 생성하지 않는다. 설정집·과거 버전은 지원 범위가 아니다.

처음부터 특정 작품만 원하는 경우 배포 전에 기존 `retrieval.env`에 `RETRIEVAL_BACKFILL_WORK_IDS`와
해당 범위를 명시할 수 있다. 자동화는 기존 allowlist를 모든 작품으로 넓히지 않는다.
기존 `worker.env`의 분석용 `EMBEDDING_GENERATION_ENABLED`는 변경하지 않는다.
`RETRIEVAL_PREPARATION_ENABLED=true`도 필수가 아니며 실행기가 현재 원고의 검색 청크를 직접 준비한다.

## 비밀값·재실행·배포 경합

- `CHAT_CAPABILITY_SECRET`은 API 호스트에서 생성·보존하며 AI나 GitHub에 전송하지 않는다.
- 검색 비밀번호·DSN은 AI의 `.chat-bootstrap/` 및 비공개 env에만 보관한다.
  DB 계정 생성 전에 비밀번호 intent를 저장해, 중간 실패 후 재실행에서도 같은 자격 증명을 사용한다.
- 동명의 기존 DB 역할이 자동화 소유로 확인되지 않으면 암호를 강제로 변경하지 않는다.
  기존 검색 DSN도 역할 속성·멤버십·쓰기 권한을 검사한다. 조회 중 `read_only`만 켜는 것으로 관리자 계정을 허용하지 않는다.
  자동 생성한 계정의 저장 DSN은 private intent·대상 DB·역할 marker를 확인하고 허용 테이블 밖의
  테이블/열 SELECT와 시퀀스 SELECT도 검사한다. 관리 증명이 일부만 남은 계정을 custom 계정으로 간주하지 않는다.
- 기존 파일의 관련 없는 항목과 custom 값을 보존한다. 최초 활성화 기록 후 운영자가 끈 flag를 다음 배포에서 다시 켜지 않는다.
- 과거 서비스별 env에 복사한 공통 API 주소·키·provider 키는 `worker.env`와 같아야 한다.
  백필의 DB 주소·S3 bucket·region도 대조해, 값이 다르면 DB 역할 준비와 AI 서비스 갱신 전에 중단한다.
  같은 중복 값은 허용하며 비밀값을 로그에 출력하거나 자동 덮어쓰지 않는다.
- API 배포와 최초 설정/공개 전환은 `.api-deploy.lock`을 공유한다. 이미지 값·다른 서비스 설정을 덮어쓰지 않는다.
- SSM/GitHub 로그에는 고정 상태와 집계만 반환한다. 원시 Docker 설정·SSM stderr·DSN·원문·비밀번호를 출력하지 않는다.
- 운영 상태 파일과 SQLite 볼륨은 삭제하지 않는다. 불확실 호출의 자동 재결제나 실패 시 전체 초기화를 하지 않는다.

## 권한·자원 부족은 자동 확대하지 않는다

기존 배포 주체에는 EC2 인스턴스/보안 그룹 조회, Worker SG의 좁은 ingress 추가,
API·Worker 두 인스턴스의 SSM 명령 실행/결과 조회 권한이 필요하다.
권한이 없으면 해당 단계가 고정 오류 코드로 멈추며 IAM 권한을 스스로 확대하지 않는다.
기존 worker DB 계정에는 검색 계정 생성과 필요한 SELECT 부여 권한이 있어야 한다.
백필 preflight는 조회·검색 테이블 쓰기·작품 행 잠금 권한과 read-only 세션 여부를 실제 데이터 변경 없이 검사한다.
운영 권한 충족 여부는 실제 배포 전 확인해야 하며 로컬 합성 테스트 통과가 운영 IAM 검증을 대신하지 않는다.

검색 계정은 실제 DB CONNECT, public USAGE와 `retrieval_chunks`, `retrieval_sources`,
`retrieval_current_chunks`, `episodes`, `works`, `members`, `episode_source_purge_requests` SELECT가 필요하다.
다른 공유 테이블의 쓰기, 객체 소유권, 높은 역할 권한을 부여하지 않는다.
DB의 PUBLIC CREATE 권한처럼 기존 공유 ACL 때문에 읽기 전용을 보장할 수 없으면 전역 ACL을 임의로 변경하지 않는다.

현재 최대 연결 한도와 예약 연결을 확인하고 앱 합계 외 관리 여유를 남긴다.
다른 앱의 미래 연결 수·전체 부하를 보증하지 않으며 RDS 용량·기존 워커 수·풀은 자동 변경하지 않는다.
AI 호스트의 가용 메모리가 부족한 경우도 보고하고 중단한다. 새 EC2나 유료 인스턴스 증설을 자동 수행하지 않는다.
기존 AI→API 8080, RDS/S3 접근과 provider 자격 증명은 기존 운영 구성을 재사용한다.

## 백필 생명주기와 복구

300초는 내부 작업·임베딩 호출에 적용하는 deadline이다. Docker 기동, 진행 중인 DB 저장과
사용량 보고·종료까지 포함한 프로세스 전체의 벽시계 300초를 보장하지는 않는다.
호스트의 Compose 호출은 780초, systemd는 900초와 종료 유예 45초를 외부 보호 한도로 둔다.
Docker나 종료 처리가 지연되면 실행 중인 같은 one-shot의 다음 5분 timer는 건너뛴다.
엄격한 컨테이너 수명 제한은 컨테이너 종료 확인과 불확실 사용량 복구를 함께 설계해야 한다.

한 순회 시작 시 작품·회차 번호 상한·생성 시점을 고정하고 `(episode_no, id)` 키셋으로 순회한다.
임베딩 호출 전 Spring 등록·시작·예약에서 일시 오류가 난 경우, 실제 provider 시도가 늘지 않았음을
확인한 때만 불확실 표식을 해제하고 다음 timer에서 다시 시도한다. 연속 세 번 실패하면 중단한다.
인증·권한 등 영구 거절은 즉시 중단하고, 어느 경우도 이미 예약한 누적 입력 상한은 환급하지 않는다.
임베딩 호출 후 오류·취소·저장 실패의 불확실 보호는 유지한다. 정상 순회 완료는 EMPTY도 무진행 횟수를 초기화한다.
회차의 현재 청크가 모두 READY인 경우만 전진하며, 시간/청크 제한 뒤 같은 위치에서 계속한다.
다음 순회는 새 원고·수정분을 찾고 완료 청크는 재호출하지 않는다.

입력 상한은 provider 호출 전에 SQLite에 기록하고 실패/재시작으로 환급하지 않는다.
응답·DB 저장 사이 종료 등 결과가 불확실하면 `BLOCKED / UNKNOWN_USAGE`로 멈춘다.
기존 사용량 보고를 먼저 복구하고 새 유료 호출을 진행한다. 외부 API와 DB 사이의 exactly-once는 제공하지 않는다.
영속 `retrieval_outbox` 볼륨의 `retrieval.sqlite`와 `backfill.sqlite3`는 완료 후에도 보존한다.

정상 배포 뒤 AI 서버의 진단 명령:

```bash
sudo -iu ubuntu
cd /opt/catchhole
python3 current-chat-release/manage-ai-runtime.py status
systemctl list-timers catchhole-retrieval-backfill.timer
systemctl show catchhole-retrieval-backfill.service -p Result -p ExecMainStatus
sudo journalctl -u catchhole-retrieval-backfill.service -n 30 --no-pager
```

첫 성공 전에는 `current-chat-release`가 없다. one-shot 서비스는 실행 사이 `inactive`여도 정상이다.
`READY`는 해당 순회에서 확인한 현재 원문 범위의 준비이며 이후 변경/검색 품질까지 보증하지 않는다.
`TIME_LIMIT`/`CHUNK_LIMIT`은 다음 주기에 이어지고, `INDEX_BUSY`는 동시 실행 중이다.
`PARTIAL`, `REPORTS_PENDING`, `BLOCKED`, `BUDGET_EXHAUSTED`는 자동 첫 공개 조건을 충족하지 않는다.
대상이 실제로 없는 완료 순회만 EMPTY로 인정한다.

첫 준비 대기 시간이 초과되면 timer와 진행 상태를 유지하고 공개 전환을 보류한다.
문제를 해결하거나 백필이 끝난 뒤 **최신 main에 해당하는 Deploy Worker EC2 실행을 재실행**하면
보존된 설정·진행 상태로 이어간다. 새 커밋/새 campaign/수동 SQL은 필요하지 않다.
이미 설정 파일을 ON으로 저장한 뒤 컨테이너 재기동이 실패한 경우에는 자동 OFF 롤백을 하지 않는다.
이 경우 env와 실제 실행 상태를 대조해 복구해야 하며, 모든 배포 실패가 공개 OFF를 보장한다고 해석하지 않는다.

불확실 상태의 재개는 원장·DB 확인 뒤에만 실행한다. 누적 예산은 초기화하지 않으며 중복 호출 가능성이 남는다.

```bash
python3 current-chat-release/manage-ai-runtime.py resume
sudo systemctl start --no-block catchhole-retrieval-backfill.service
```

새 유료 백필 중단은 `retrieval.env`의 `RETRIEVAL_BACKFILL_ENABLED=false`와 API의
`CHAT_OPERATIONS_ENABLED=false`로 제어한다. timer를 유지하면 disabled 상태에서도 outbox 복구가 계속된다.
공개 중단은 API의 `CHAT_ENABLED=false`로 별도 제어한다. API/chatbot env 변경에는 컨테이너 재생성이 필요하고,
retrieval timer는 다음 실행 때 env를 다시 읽는다. 이미 전송된 공급자 호출은 취소되지 않는다.

## 검증 경계

테스트는 합성 데이터·격리 PostgreSQL로 수행하고 운영 키나 유료 모델을 호출하지 않는다.
사전 점검은 설정·인증·스키마·SELECT/역할 권한·자원을 확인한다. 실제 모델 접근 권한, S3 GetObject,
답변 품질을 보증하지 않는다. 운영 배포 뒤 등록 설정 질문·원문 질문·인용 열기를 별도로 확인한다.
