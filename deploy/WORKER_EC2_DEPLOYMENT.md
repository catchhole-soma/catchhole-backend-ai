# CatchHole 인공지능 작업 서버 운영 배포

이 문서는 Worker 서버용 Amazon EC2 인스턴스에서 설정 추출 Worker 5개, 캐릭터 비교 Worker 1개, 세계관 비교 Worker 1개를 실행하는 절차를 설명한다. 검수 Worker는 기본 0개이며 [검수 Worker 켜기](#검수-worker-켜기-344)로 명시적으로 켠다. API 서버, Caddy, Redis, PostgreSQL은 이 서버에서 실행하지 않는다.

## 서버 파일

Worker 서버에는 다음 파일을 둔다.

```text
/opt/catchhole
├── compose.worker.prod.yml
└── worker.env
```

- `compose.worker.prod.yml`은 인공지능 작업 서버 저장소에서 내려받는다.
- `worker.env`는 `deploy/worker.env.example`을 기준으로 서버에서 직접 작성하고 커밋하지 않는다.
- `AI_IMAGE`에는 발행이 성공한 이미지의 `sha-<short-sha>` 태그를 넣는다. 자동 배포는 이 값을 배포 대상 SHA로 갱신한다.
- AWS 액세스 키와 비밀 액세스 키는 `worker.env`에 넣지 않는다. Amazon EC2 인스턴스 역할을 사용한다.

## 로컬 로그 보관

운영 컨테이너는 표준 출력과 표준 오류를 Docker `journald` 로그 드라이버로 전달한다. systemd journal은 Worker 서버용 Amazon EC2 인스턴스의 로컬 디스크에 최대 14일 또는 1GB까지만 보관한다. 컨테이너 재생성과 인스턴스 재부팅 후에도 로그를 조회할 수 있지만, 인스턴스 또는 루트 Amazon EBS 볼륨을 삭제하면 로그도 삭제된다.

Worker 서버에서 다음 설정을 적용한다.

```bash
sudo install -d -m 0755 /etc/systemd/journald.conf.d
```

```bash
sudo tee /etc/systemd/journald.conf.d/catchhole.conf > /dev/null <<'EOF'
[Journal]
Storage=persistent
SystemMaxUse=1G
MaxRetentionSec=14day
Compress=yes
EOF
```

```bash
sudo install -d -m 2755 /var/log/journal
```

```bash
sudo systemd-tmpfiles --create --prefix /var/log/journal
```

```bash
sudo systemctl restart systemd-journald
```

```bash
sudo journalctl --flush
```

설정과 현재 사용량을 확인한다.

```bash
sudo cat /etc/systemd/journald.conf.d/catchhole.conf
```

```bash
sudo journalctl --disk-usage
```

## Worker 서버 인스턴스 역할과 메타데이터 설정

Worker 컨테이너는 고정 AWS 액세스 키 대신 Worker 서버용 Amazon EC2 인스턴스 역할로 Amazon S3에 접근한다. Worker 서버용 Amazon EC2 인스턴스에 필요한 Amazon S3 버킷 읽기·쓰기 권한이 있는 역할을 연결한다.

Docker 브리지 네트워크 안의 컨테이너가 Instance Metadata Service Version 2 응답을 받을 수 있도록 Worker 서버용 Amazon EC2 인스턴스의 메타데이터 응답 홉 제한을 `2`로 설정한다. Amazon EC2 콘솔에서 인스턴스를 선택한 뒤 **작업 → 인스턴스 설정 → 인스턴스 메타데이터 옵션 수정**에서 다음과 같이 설정한다.

- Instance Metadata Service: 활성화
- Instance Metadata Service Version 2: 필수
- 메타데이터 응답 홉 제한: `2`

AWS Command Line Interface로 수정할 때는 아래 인스턴스 ID를 실제 Worker 서버용 Amazon EC2 인스턴스 ID로 바꾼다.

```bash
aws ec2 modify-instance-metadata-options \
  --instance-id replace-with-worker-ec2-instance-id \
  --http-endpoint enabled \
  --http-tokens required \
  --http-put-response-hop-limit 2 \
  --region ap-northeast-2
```

적용 상태를 확인한다.

```bash
aws ec2 describe-instances \
  --instance-ids replace-with-worker-ec2-instance-id \
  --region ap-northeast-2 \
  --query 'Reservations[0].Instances[0].MetadataOptions.{Endpoint:HttpEndpoint,Tokens:HttpTokens,HopLimit:HttpPutResponseHopLimit,State:State}' \
  --output table
```

`Endpoint=enabled`, `Tokens=required`, `HopLimit=2`, `State=applied`여야 한다. 호스트에서의 AWS 자격 증명 검증만으로 대체하지 않고, Worker 서버에서 Docker 컨테이너를 직접 실행해 역할과 Amazon S3 권한을 확인한다. 버킷 이름은 `worker.env`의 `AWS_S3_BUCKET` 실제 값으로 바꾼다.

```bash
sudo docker run --rm \
  -e AWS_REGION=ap-northeast-2 \
  public.ecr.aws/aws-cli/aws-cli:latest \
  sts get-caller-identity
```

```bash
sudo docker run --rm \
  -e AWS_REGION=ap-northeast-2 \
  public.ecr.aws/aws-cli/aws-cli:latest \
  s3api get-bucket-location \
  --bucket replace-with-s3-bucket-name \
  --region ap-northeast-2
```

첫 번째 명령은 Worker 서버용 인스턴스 역할의 ARN을 포함한 응답을 반환해야 하고, 두 번째 명령은 버킷 위치를 오류 없이 반환해야 한다. `Unable to locate credentials`가 나오면 인스턴스 역할 연결과 메타데이터 홉 제한을 다시 확인한다. `AccessDenied`가 나오면 인스턴스 역할의 Amazon S3 정책을 확인한다.

## 네트워크와 인증 계약

`worker.env`의 API 서버 주소에는 API 서버용 Amazon EC2 인스턴스의 사설 IPv4 주소를 사용한다.

```dotenv
SPRING_INTERNAL_API_BASE_URL=http://replace-with-api-private-ip:8080
SPRING_INTERNAL_API_KEY=replace-with-the-same-internal-api-key-as-the-api-server
SPRING_WORKER_READ_TIMEOUT_SECONDS=120
```

`SPRING_INTERNAL_API_KEY`는 API 서버 `api.env`의 `INTERNAL_API_KEY`와 정확히 같은 값이어야 한다.

`SPRING_WORKER_READ_TIMEOUT_SECONDS`는 분석 상태 API가 서버에서 상태 재구성·저장을
수행하는 동안 응답을 기다릴 read 한도다. 기본 120초이며 유한한 양수만 허용한다.
최초 claim, 후보 게시/비교 claim, 문맥 조회, progress, complete, fail에 적용한다.
연결·쓰기·connection pool 대기와 heartbeat·token 원장의 read 한도는 각각 기존 30초다.
Compose의 세 Worker 서비스는 같은 설정을 사용한다. 일반 분석 POST 재전송은 추가하지 않는다.

120초는 HTTPX가 응답 헤더/다음 바이트를 기다리는 한 번의 읽기 제한이다.
요청 전체나 Job 완료의 절대 시간 보장이 아니다. heartbeat는 기존 60초 주기,
최대 3회 시도와 재시도 간격 2/4초를 유지하며 최종 실패하면 소유 Job을 취소한다.
예를 들어 같은 Job의 DB 잠금으로 heartbeat도 매번 read 30초를 소진하면,
첫 heartbeat 시도부터 약 96초 뒤 취소할 수 있다(다른 I/O 대기는 별도).
read를 늘려도 lease 소유권 검사·회수와 이 취소 경계는 바꾸지 않는다.
운영 적용 후 timeout 수뿐 아니라 claim/context/complete 시간, heartbeat 실패,
SQL·상태 재구성 시간을 함께 확인한다. HTTP 여유는 서버 병목 최적화를 대신하지 않는다.

Worker 서버에서 실제 주소로 연결을 확인한다.

```bash
curl -fsS http://replace-with-api-private-ip:8080/actuator/health
```

연결되지 않으면 다음 항목을 확인한다.

1. API 서버에 `catchhole-api-prod-sg` 보안 그룹이 연결되어 있는지 확인한다.
2. Worker 서버에 `catchhole-worker-prod-sg` 보안 그룹이 연결되어 있는지 확인한다.
3. API 서버 보안 그룹의 TCP 8080번 인바운드 소스가 Worker 서버 보안 그룹인지 확인한다.
4. API 서버 Docker Compose가 `8080:8080` 포트를 게시하는지 확인한다.

## Amazon RDS 연결 계약

Worker의 PostgreSQL 연결 문자열은 다음 형식을 사용한다.

```dotenv
APP_TIMEZONE=Asia/Seoul
DATABASE_URL=postgresql+psycopg://catchhole_admin:replace-with-url-encoded-password@replace-with-rds-endpoint:5432/catchhole?sslmode=require
DATABASE_POOL_SIZE=3
DATABASE_POOL_MAX_OVERFLOW=0
```

사용자 이름 또는 비밀번호에 `@`, `:`, `/`, `?`, `#`, `%` 같은 예약 문자가 있으면 URL 백분율 인코딩을 적용해야 한다. 원래 비밀번호를 바꾸는 것이 아니라 연결 문자열에 넣는 표현만 인코딩한다.

Worker 서버에서 PostgreSQL 클라이언트로 연결을 확인한다. 아래 명령의 엔드포인트와 사용자 이름은 실제 값으로 바꾸며, 비밀번호는 프롬프트에서 입력한다.

```bash
psql "host=replace-with-rds-endpoint port=5432 dbname=catchhole user=catchhole_admin sslmode=require" -W
```

접속한 PostgreSQL 프롬프트에서 시간대를 확인한다.

```sql
SHOW timezone;
```

결과는 `Asia/Seoul`이어야 한다. Amazon RDS 파라미터 그룹은 API 서버 배포 문서의 절차에 따라 `timezone=Asia/Seoul`로 설정한다. Worker는 또한 SQLAlchemy가 새 PostgreSQL 연결을 만들 때마다 `worker.env`의 `APP_TIMEZONE`을 session 연결 옵션으로 전달한다.

## 50개 작업 슬롯

기본 운영값은 다음과 같다.

```dotenv
AI_WORKER_PROCESS_COUNT=5
AI_WORKER_CONCURRENCY=10
LLM_MAX_CONCURRENT_REQUESTS=10
AI_WORKER_BLOCKING_MAX_WORKERS=3
```

- 설정 추출 Worker 컨테이너는 5개다.
- 각 컨테이너는 동시에 최대 10개 분석 작업을 실행한다.
- 설정 추출 작업 슬롯은 `5 × 10 = 50`개다.
- 캐릭터 비교 Worker와 세계관 비교 Worker는 각각 1개이며 동시 작업과 언어 모델 요청을 각각 1개로 고정한다.
- `LLM_MAX_CONCURRENT_REQUESTS=10`은 각 설정 추출 Worker 컨테이너 안의 상한이다. 전체 서버 또는 OpenAI 계정에 대한 전역 상한은 아니다.

## 검수 Worker 켜기 (#344)

`compose.worker.prod.yml`의 `ai-validation-worker`는 검수 전용 runner(`scripts/run_validation_worker.py --enable`)를 연속 실행한다. 기본 `VALIDATION_WORKER_PROCESS_COUNT=0`이라 컨테이너를 만들지 않는다. 분석 Worker와 같은 이미지·`SPRING_INTERNAL_API_BASE_URL`·`SPRING_INTERNAL_API_KEY`·`LLM_API_KEY`를 쓰고, S3·metrics port·tracing은 쓰지 않는다. DB는 [의미 검색 켜기](#의미-검색-켜기-선택)를 했을 때만 읽기 전용으로 쓴다. 기본(키워드 검색만)에서는 데이터베이스 연결 수 예산과 Prometheus target 수가 바뀌지 않는다.

전제: Backend 저장소 `deploy/EC2_DEPLOYMENT.md`의 검수 실행 켜기 절차로 API 서버 `api.env`에 `VALIDATION_EXECUTION_ENABLED`와 검수 예산(`SPRING_APPLICATION_JSON`)을 먼저 적용하고 API가 정상 응답해야 한다. API 쪽이 꺼져 있으면 Worker를 켜지 않는다.

`worker.env`에 다음 값을 둔다. 처음에는 프로세스 1개로 켠다.

```dotenv
VALIDATION_WORKER_PROCESS_COUNT=1
VALIDATION_WORKER_CONCURRENCY=3
VALIDATION_WORKER_LLM_CONCURRENCY=4
VALIDATION_WORKER_POLL_SECONDS=5
```

- `VALIDATION_WORKER_CONCURRENCY`는 프로세스당 동시 검수 Job 수, `VALIDATION_WORKER_LLM_CONCURRENCY`는 그 프로세스 전체의 provider 동시 요청 상한이다. 컨테이너 안에서는 각각 `AI_WORKER_CONCURRENCY`, `LLM_MAX_CONCURRENT_REQUESTS`로 전달되며 분석 Worker 값과 분리된다.
- 같은 provider key를 쓰므로 계정 전체 동시 요청은 분석 50(5×10) + 비교 2 + 검수 프로세스 수 × `VALIDATION_WORKER_LLM_CONCURRENCY`의 합이다.
- `VALIDATION_WORKER_POLL_SECONDS`는 claim 시도 사이 간격이며 연속 실패 시 최대 60초까지 늘어난다.
- 선택 값 `VALIDATION_WORKER_ID`(기본 `validation-prod`)는 claim의 `workerId`다. 프로세스를 2개 이상으로 늘려 같은 ID를 공유해도 Backend가 claim마다 Job을 잠그고 lease token을 따로 발급하므로 같은 Job을 중복 실행하지 않는다.

렌더링을 확인한 뒤 검수 Worker만 생성한다. 분석·비교 Worker는 재생성하지 않는다.

```bash
cd /opt/catchhole
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml config --quiet
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml up -d ai-validation-worker
```

실행 상태와 로그를 확인한다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml ps
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml logs --tail=100 ai-validation-worker
```

정상이면 대기 중에는 `validation runner` 로그가 조용하고, Job을 claim해 처리하면 `setting inspection completed`(오탈자 검사는 `typo inspection completed`) 줄이 남는다. `validation job failed`, `validation job incomplete`, `validation runner backoff`가 반복되면 API 쪽 검수 실행 설정·예산과 내부 키를 확인한다. 로그에는 고정 코드와 job/report ID만 남고 원문·응답 본문은 남지 않는다.

끄려면 `worker.env`의 `VALIDATION_WORKER_PROCESS_COUNT=0`으로 되돌리고 검수 Worker만 0개로 줄인다. 다른 Worker의 대기 중인 설정 변경까지 함께 적용되지 않도록 전체 `up -d`나 `--remove-orphans`로 끄지 않는다.

```bash
sudo sed -i -e 's/^VALIDATION_WORKER_PROCESS_COUNT=.*/VALIDATION_WORKER_PROCESS_COUNT=0/' /opt/catchhole/worker.env
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml up -d ai-validation-worker --scale ai-validation-worker=0
```

급히 멈출 때는 `stop`을 쓴다. 이 방법은 다음 `up -d`나 자동 배포에서 다시 시작되므로 위처럼 `worker.env`도 0으로 바꿔 둔다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml stop -t 210 ai-validation-worker
```

어느 방법이든 SIGTERM을 받으면 새 claim을 멈추고 실행 중 Job에 180초(`AI_WORKER_SHUTDOWN_GRACE_SECONDS`)를 준 뒤 남은 Job을 취소한다. 취소한 Job은 실패로 보고하지 않고 Backend lease 만료와 재claim으로 복구된다. Compose 종료 유예는 분석 Worker와 같은 210초다.

GitHub Actions 자동 배포의 `manage-ai-runtime.py`는 같은 SHA의 Compose에 `ai-validation-worker`가 있어야 진행하고, 이 서비스의 내부 API 주소·내부 키·provider key가 `ai-worker`와 같은지 검사한다. 배포 뒤에는 `VALIDATION_WORKER_PROCESS_COUNT`만큼 검수 컨테이너가 실행될 때까지 기다리며, 0이면 실행 중인 검수 컨테이너가 없는지만 확인한다. 결과 JSON의 `validationReplicas`로 적용된 수를 확인한다.

### 의미 검색 켜기 (선택)

기본 검수 Worker는 과거 원고를 키워드로만 찾는다. 챗봇이 쓰는 원문 색인(`retrieval_chunks`)으로 의미(벡터) 검색까지 쓰려면 아래 순서로 켠다. Compose는 `--readonly-url-env VALIDATION_READONLY_DATABASE_URL --query-embedding --index-version`을 항상 넘기며, URL 값이 비어 있으면 runner가 `frozen retrieval disabled: VALIDATION_READONLY_DATABASE_URL is empty`를 남기고 키워드 검색만 쓴다.

1. **API 예산.** Backend 저장소 `deploy/EC2_DEPLOYMENT.md`의 검수 실행 절차로 API 서버 검수 예산 JSON(`SPRING_APPLICATION_JSON`)에 `roles.QUERY_EMBEDDING`(최대 호출 수·예약 token)과 `query-embedding-model`(`text-embedding-3-small`)을 넣고 API를 재기동한다. Backend `deploy/validation-execution.prod.example.json`에 예시가 있다. 이 역할이 없으면 URL을 넣어도 Job마다 키워드 검색만 제안한다(실패하지 않는다).
2. **DB 연결 예산.** 켜면 검수 프로세스마다 읽기 전용 연결이 `VALIDATION_WORKER_CONCURRENCY`개(기본 3) 더 생긴다. 실제 RDS 여유를 확인하고 `retrieval.env`의 `AI_RUNTIME_DB_CONNECTION_BUDGET`을 `검수 프로세스 수 × VALIDATION_WORKER_CONCURRENCY`만큼 올린다(예: 32 → 35). 자동 배포는 이 값을 넘으면 `DATABASE_CONNECTION_BUDGET_EXCEEDED`로 멈춘다.
3. **읽기 전용 계정 권한.** 검수 검색은 챗봇 계정(`catchhole_chat_reader`)으로 챗봇 7개 테이블 외에 `validation_reports`·`validation_jobs`·`validation_baseline_sources`도 읽는다. 챗봇 bootstrap은 이 세 테이블 SELECT를 추가한 AI SHA부터 배포 때마다 관리 계정의 grant를 갱신한다(초과 권한은 계속 거절). **검수 opt-in 전에 bootstrap을 다시 실행해 reader grant를 갱신한다**: 이 변경이 포함된 SHA의 GitHub Actions `Deploy Worker EC2`(최초 설정 단계 포함)가 성공했는지 확인하고, 아니면 그 실행을 다시 실행한다. 수동 `GRANT`는 bootstrap이 `READ_ROLE_EXCESS_READ_PRIVILEGE`로 거절하므로 하지 않는다. 운영자가 직접 만든(custom) 검색 계정이면 세 테이블 SELECT를 따로 준다. 권한이 없으면 Job마다 `vector search readiness unavailable error=ProgrammingError` 경고와 함께 그 Job은 frozen RAG 없이 키워드로만 진행한다. 자세한 계정 정책은 `docs/chat-runtime-operations.md`의 권한 절을 따른다.
4. **worker.env.** `VALIDATION_READONLY_DATABASE_URL`에 `chatbot.env`의 `CHAT_RETRIEVAL_DATABASE_URL`과 같은 SELECT 전용 계정 URL을 그대로 넣고, `VALIDATION_WORKER_INDEX_VERSION`은 `CHAT_RETRIEVAL_EMBEDDING_VERSION`(기본 `v1`)과 맞춘다. 값이 터미널·셸 기록에 남지 않도록 편집기로 넣는다.

```bash
sudoedit /opt/catchhole/worker.env
```

5. **재기동.** 렌더링을 확인하고 검수 Worker만 다시 만든다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml config --quiet
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml up -d ai-validation-worker
```

6. **확인.** 시작 로그에 `frozen retrieval disabled`가 없어야 하고, Job을 처리하면 `vector search offered=True sources=<n> ready=<n> ...`이 남는다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml logs --tail=200 ai-validation-worker | grep -E 'frozen retrieval disabled|query embedding disabled|vector search'
```

- `vector search offered=False`이고 `ready`가 `sources`보다 작으면 그 작품의 일부 회차가 아직 색인되지 않은 것이다. 그 Job은 키워드 검색만으로 정상 진행하며, 색인 백필이 끝난 뒤의 Job부터 의미 검색이 제안된다.
- `embedding=False`이면 API 예산 JSON에 `QUERY_EMBEDDING`이 없다(1단계).
- `vector search readiness unavailable error=<예외 종류>`는 읽기 전용 DB 접속·권한 문제다(`ProgrammingError`는 3단계 권한, `TimeoutError`는 5초 안에 응답하지 않은 DB). 그 Job은 frozen RAG 없이 키워드 검색만으로 계속한다. 로그에는 예외 종류와 Job ID만 남는다.

끄려면 `VALIDATION_READONLY_DATABASE_URL`을 다시 비우고 5단계로 검수 Worker만 재기동한다. 자동 배포의 `manage-ai-runtime.py`는 URL이 비어 있으면 아무것도 검사하지 않고, 값이 있으면 챗봇의 `CHAT_RETRIEVAL_DATABASE_URL`과 같은지(`INCONSISTENT_VALIDATION_READONLY_DATABASE_URL`), `--index-version`이 `CHAT_RETRIEVAL_EMBEDDING_VERSION`과 같은지(`INCONSISTENT_VALIDATION_WORKER_INDEX_VERSION`) 확인한 뒤 위 연결 수를 예산에 넣는다. 결과 JSON의 `validationRetrieval`로 켜짐 여부를 확인한다.

## 데이터베이스 연결 수 예산

| 실행 주체 | 프로세스 수 | 프로세스당 최대 연결 수 | 합계 |
| --- | ---: | ---: | ---: |
| Spring Backend | 1 | 10 | 10 |
| 설정 추출 Worker | 5 | 3 | 15 |
| 캐릭터 비교 Worker | 1 | 1 | 1 |
| 세계관 비교 Worker | 1 | 1 | 1 |
| 전체 |  |  | 27 |

SQLAlchemy의 `DATABASE_POOL_MAX_OVERFLOW=0`은 기본 연결 풀을 넘는 임시 연결 생성을 막는다. 전체 애플리케이션의 이론상 최대 연결 수는 27개로, NVM-315의 40개 이하 기준을 만족한다.

검수 Worker는 기본 0개이고 키워드 검색만 쓰면 DB에 연결하지 않는다. [의미 검색](#의미-검색-켜기-선택)을 켜면 검수 프로세스 수 × `VALIDATION_WORKER_CONCURRENCY`개(기본 1 × 3 = 3)의 읽기 전용 연결(overflow 0)이 더해지므로, 챗봇·색인 백필 연결과 함께 `AI_RUNTIME_DB_CONNECTION_BUDGET`과 실제 RDS 한도를 확인한다.

## 최초 실행

Worker 서버에서 다음 순서로 실행한다.

```bash
cd /opt/catchhole
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml config --quiet
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml pull
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml up -d
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml ps
```

설정 추출 Worker가 5개인지 확인한다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml ps --status running -q ai-worker | wc -l
```

결과는 `5`여야 한다.

캐릭터 비교 Worker와 세계관 비교 Worker가 각각 1개인지 확인한다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml ps --status running -q ai-character-comparison-worker | wc -l
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml ps --status running -q ai-world-comparison-worker | wc -l
```

두 결과 모두 `1`이어야 한다.

모든 Worker 컨테이너의 로그 드라이버가 `journald`인지 확인한다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml ps -q \
  | xargs -r sudo docker inspect --format '{{.Name}} {{.HostConfig.LogConfig.Type}}'
```

현재 Worker 로그는 Docker Compose로 확인한다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml logs --tail=100
```

컨테이너 재생성 전 로그를 포함한 서버 보관 로그는 systemd journal에서 Docker 컨테이너 이름으로 확인한다.

```bash
sudo journalctl CONTAINER_NAME=catchhole-worker-ai-worker-1 --since '14 days ago' --no-pager
```

## 25개 작업 슬롯으로 낮추기

OpenAI 요청 제한, Amazon RDS 연결 지연, CPU 사용률, 메모리 사용률 또는 API 서버 응답 시간이 허용 기준을 넘으면 설정 추출 Worker 수는 5개로 유지하고 컨테이너당 작업 슬롯을 5개로 낮춘다.

먼저 `worker.env`의 현재 동시성 설정을 백업하고 두 값을 5로 변경한다.

```bash
sudo sed -i.bak -e 's/^AI_WORKER_CONCURRENCY=.*/AI_WORKER_CONCURRENCY=5/' -e 's/^LLM_MAX_CONCURRENT_REQUESTS=.*/LLM_MAX_CONCURRENT_REQUESTS=5/' /opt/catchhole/worker.env
```

렌더링 오류가 없는지 확인한다.

```bash
cd /opt/catchhole
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml config --quiet
```

설정 추출 Worker만 재생성한다. 실행 중인 작업은 내부적으로 최대 180초 동안 종료를 기다리고 Docker Compose는 최대 210초를 허용한다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml up -d --force-recreate ai-worker
```

실제 주입값을 확인한다.

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml exec ai-worker printenv AI_WORKER_CONCURRENCY
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml exec ai-worker printenv LLM_MAX_CONCURRENT_REQUESTS
```

두 결과가 모두 `5`면 `5 × 5 = 25`개 작업 슬롯이 적용된 것이다. 캐릭터 비교 Worker와 세계관 비교 Worker는 재생성하지 않는다.

## 50개 작업 슬롯으로 복구하기

원인이 해소되고 부하 검증 기준을 다시 만족하면 두 값을 10으로 되돌린다.

```bash
sudo sed -i -e 's/^AI_WORKER_CONCURRENCY=.*/AI_WORKER_CONCURRENCY=10/' -e 's/^LLM_MAX_CONCURRENT_REQUESTS=.*/LLM_MAX_CONCURRENT_REQUESTS=10/' /opt/catchhole/worker.env
```

```bash
cd /opt/catchhole
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml config --quiet
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml up -d --force-recreate ai-worker
```

## 안전하게 전체 Worker 종료하기

배포 또는 장애 대응 전에 신규 작업 가져오기를 중단하고 실행 중인 작업의 종료를 기다린다.

```bash
cd /opt/catchhole
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml stop -t 210 ai-worker ai-character-comparison-worker ai-world-comparison-worker ai-validation-worker
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml ps
```

## 이전 인공지능 작업 이미지로 되돌리기

`worker.env`의 `AI_IMAGE`를 배포 전 기록한 SHA 태그로 변경한다.

```dotenv
AI_IMAGE=ghcr.io/catchhole-soma/catchhole-backend-ai:sha-replace-with-previous-short-sha
```

그다음 전체 Worker 이미지를 내려받아 재생성한다.

```bash
cd /opt/catchhole
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml pull
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml up -d --force-recreate
```

```bash
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml ps
```

롤백 원인을 해결한 뒤에는 GitHub Actions에서 복구할 SHA에 대응하는 성공한 `Deploy Worker EC2` 실행을 다시 실행한다. 이 Workflow는 해당 publish run의 SHA 태그로 `worker.env`를 갱신한 뒤 전체 Worker를 재배포한다. 이후 새로운 `main` 이미지 발행이 성공해도 같은 방식으로 `AI_IMAGE`가 새 SHA로 자동 갱신되므로 롤백 이미지가 다음 자동 배포에 남지 않는다.

## GitHub Actions 자동 배포

`.github/workflows/deploy-worker-ec2.yml`은 `main` push에서 시작된 `Publish AI Image`가 성공했을 때만 Worker 서버용 Amazon EC2 인스턴스를 배포한다. 수동 이미지 발행은 Worker 배포로 이어지지 않는다.

배포 전에 Backend 저장소의 최신 `main` 커밋에 대응하는 `Deploy API EC2` 실행이 성공했는지 확인하고 `https://api.catchhole.com/actuator/health`가 응답할 때까지 최대 15분 기다린다. 조건을 충족하지 못하면 Worker 서버에 Systems Manager 명령을 보내지 않는다. 따라서 Backend PR을 먼저 병합하고 Flyway·API 배포가 성공한 다음 AI PR을 병합해야 한다.

배포 Workflow는 `Publish AI Image` 실행의 commit SHA를 기준으로 `compose.worker.prod.yml`을 내려받고 같은 SHA의 `sha-<short-sha>` 이미지 태그를 `worker.env`에 기록한다. `main` 태그나 실행 시점의 최신 Compose를 사용하지 않으므로 서로 다른 커밋의 이미지와 설정이 섞이지 않는다.

인공지능 작업 서버 저장소의 GitHub Actions 비밀값은 다음 이름을 사용한다.

```text
AWS_REGION=ap-northeast-2
WORKER_EC2_INSTANCE_ID=replace-with-worker-ec2-instance-id
WORKER_EC2_DEPLOY_PATH=/opt/catchhole
WORKER_EC2_DEPLOY_USER=ubuntu
```

AWS OpenID Connect 역할을 사용하는 경우 다음 값도 설정한다.

```text
AWS_ROLE_TO_ASSUME=replace-with-github-actions-deploy-role-arn
```

기존 액세스 키 방식을 임시로 유지하는 경우 다음 두 값이 필요하다.

```text
AWS_ACCESS_KEY_ID=replace-with-access-key-id
AWS_SECRET_ACCESS_KEY=replace-with-secret-access-key
```

GitHub Actions가 사용하는 AWS Identity and Access Management 사용자 또는 역할에는 Worker 서버용 Amazon EC2 인스턴스를 대상으로 `ssm:SendCommand`를 실행하고 결과를 조회할 권한이 있어야 한다. 기존 정책에 API 서버용 인스턴스 ID만 있다면 Worker 서버용 인스턴스 ID를 별도로 추가해야 한다.

예전에 사용하던 `BACKEND_DEPLOY_TOKEN`은 더 이상 필요하지 않다. 각 저장소가 자신의 Amazon EC2 인스턴스만 배포하므로 인공지능 작업 이미지 발행이 백엔드 저장소의 통합 배포를 호출하지 않는다.

## 분석 Worker Prometheus 지표

Python CLI의 같은 프로세스에서 `prometheus_client.start_http_server`가 HTTP thread를 제공한다. 별도 FastAPI 서버는 필요 없다. 앱의 기본값은 비활성·127.0.0.1:9102이며 운영 Compose는 활성화와 컨테이너 내부 0.0.0.0:9102를 명시한다. 호스트 publish의 기본값은 localhost다.

- `AI_WORKER_METRICS_ENABLED=true`: CLI exporter 활성화.
- `AI_WORKER_METRICS_BIND_ADDRESS`: 최초 localhost, 모니터링 SG만 TCP 9102–9108 접근하도록 적용한 뒤 Worker EC2 사설 IPv4로 변경.
- `AI_WORKER_METRICS_PORT_RANGE=9102-9106`: 분석 replica 5개에 각기 다른 host port를 할당. 같은 host 고정 port 하나를 모든 replica에 지정하면 충돌한다.
- 캐릭터 비교 host 9107 / 세계관 비교 host 9108, 각 컨테이너 내부는 동일 9102.
- Worker 5×10, 비교 각 1의 동시성과 180/210초 graceful shutdown은 변경하지 않는다. scale을 바꿀 때 port range 용량과 target 예상 수·SG를 함께 검토한다.

[generate_worker_targets.py](generate_worker_targets.py)는 Docker inspect의 실제 mapping을 확인하고 일곱 개 endpoint를 생성한다. 부족한 프로세스, duplicate 주소/replica, wildcard·localhost·public host bind를 발견하면 실패한다. **운영 사설 수집용**이며 localhost 개발용 target 생성기는 아니다. 이 파일은 같은 배포 SHA의 코드와 함께 Worker 서버에 설치한다.

```bash
cd /opt/catchhole
# 배포 대상 저장소의 같은 SHA에서 deploy/generate_worker_targets.py도 복사한다.
if python3 generate_worker_targets.py --expected-analysis 5 > worker-targets.json.next && python3 -m json.tool worker-targets.json.next >/dev/null; then
  mv worker-targets.json.next worker-targets.json
fi
```

위 조건문은 생성과 검증이 모두 성공한 경우에만 targets를 교체한다. 생성 실패 시 기존 targets를 빈 파일로 교체하지 않는다. Docker 배포 사용자가 같은 프로젝트의 컨테이너를 inspect할 수 있어야 한다.

생성한 JSON을 모니터링 EC2의 `/opt/catchhole-monitoring/monitoring/targets/workers/catchhole-worker.json.next`로 전달한 뒤 검사·rename한다. JSON은 주소/port/유한한 Worker 구분만 포함하며 Worker env나 인증값을 전송하지 않는다. 다른 EC2이므로 Worker 서버의 로컬 JSON 생성만으로 수집 대상이 자동 갱신되지는 않는다. 기존 CI는 Compose/이미지를 배포하므로 운영 적용 담당자는 **매 배포 뒤 target 생성·전달 확인**을 배포 확인 항목으로 수행한다. 특히 host 주소/port range/scale 변경·컨테이너 재생성 후 다시 확인한다.

모니터링의 `up{job="catchhole-worker",environment="prod"}`에서 일곱 endpoint가 모두 1인지 확인한다. 하나가 빠졌으면 다른 정상 프로세스의 수치로 이를 가리지 않는다. 유휴 Worker는 active=0, last-finished=0(아직 종료 없음)일 수 있다. 수집 실패/port bind 실패는 분석 결과와 별도로 확인한다.

LLM Histogram은 delegate 한 호출의 monotonic 시간이며 semaphore·예약/정산·retry sleep은 제외한다. transport 재시도만 retries Counter에 기록하며 schema/출력 절단의 바깥 재시도는 호출/오류/시간으로 보인다. usage input에는 cached input이 포함된다. 이 Counter를 quota 감사 원장이나 금액으로 대체하지 않는다. 운영/로컬 labels, 지표 정의와 성공률은 Java 저장소 `docs/analysis-metrics.md`를 따른다.

LLM 호출·재시도 Counter의 `error_type`은 HTTP 400·401·403을 각각 `400`·`401`·`403`으로 구분하고 다른 4xx는 `4xx`로 묶는다. 429는 기존 `429`, 408은 `timeout`, 500~599는 `5xx`를 유지한다. 예외 wrapper가 있어도 원인 HTTP status로 분류하며 응답 본문·오류 메시지는 label에 넣지 않는다.

로컬 fake 검증:

```bash
python -m pytest tests/test_worker_metrics.py tests/test_ai_token_metering.py tests/test_run_analysis_worker.py
python -m unittest discover -s deploy/tests -p 'test_worker_targets.py'
```

실제 LLM 과금 호출 없이 지연/4xx/429/timeout/취소와 exporter HTTP를 검증한다. exporter의 생성·업데이트·bind·종료 오류가 원래 Worker·ledger 결과를 바꾸지 않도록 경고만 남긴다. rollout은 Java schema/API 배포 성공 뒤 기존 AI main 배포를 사용하며 이전 이미지로 rollback할 때 target/bind와 수집 상태도 함께 확인한다.

## Worker 트레이스 운영 적용 준비 (#271, 7단계)

이 절차는 운영 적용을 위한 설정과 확인 항목이다. 파일을 추가하거나 기본값으로 배포한
것만으로 트레이스가 켜지지 않으며, 실제 운영 활성화·데이터 조회는 별도로 확인한다.
세 Worker 서비스가 공통 설정을 상속하고 종류별 활성화 값을 우선 적용한다.
분석 replica별 포트 추가나 프로세스 수 변경은 필요 없다.

```text
분석·캐릭터 비교·세계관 비교 Worker
  └─ OTLP HTTP push → 모니터링 EC2 사설 IPv4:4318/v1/traces
                       └─ Collector → Tempo → Grafana
```

1. 모니터링 서버의 Collector·Tempo·Grafana 연결과 보존/용량 설정을 먼저 검증한다.
   Collector TCP 4318은 모니터링 EC2의 사설 주소에만 게시하고, 모니터링 보안 그룹의
   인바운드 소스를 Worker/API 서버 보안 그룹으로 제한한다. 인터넷 공개 규칙을 만들지
   않는다. Worker의 제한된 egress·NACL이 있다면 해당 사설 경로를 허용한다.
2. Spring Java Agent의 전파·개인정보 필터와 OTLP 목적지를 준비한다. Worker가 보내는
   `traceparent`는 인증 수단이 아니며 기존 내부 API 키와 lease 검증을 유지한다.
   Spring Agent가 꺼져 있으면 Python CLIENT Span 뒤의 SERVER/JDBC Span은 생기지 않는다.
3. 배포 대상 SHA의 이미지와 Compose를 준비하고 현재 환경 파일을 비공개로 백업한다.
   `worker.env`에 아래 세 값을 설정한다. 주소는 운영자가 확인한 모니터링 EC2의
   **RFC1918 IPv4**로 바꾼다. `localhost`는 Worker 컨테이너 자신이므로 사용하지 않는다.

```dotenv
# 기본값. 사설 수신기와 검증 절차가 준비되기 전에는 유지한다.
AI_WORKER_TRACING_ENABLED=false
OTEL_SERVICE_NAME=catchhole-ai-worker
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=

```

첫 canary는 기존 **캐릭터 비교 Worker 한 프로세스**만 사용한다. Collector → Spring
확인 뒤 `worker.env`를 다음 상태로 변경한다. 새 Worker를 추가하거나 분석 replica 5개를
줄이지 않으며 기존 Job/LLM 동시성도 유지한다.

```dotenv
AI_WORKER_TRACING_ENABLED=false
AI_ANALYSIS_TRACING_ENABLED=false
AI_CHARACTER_COMPARISON_TRACING_ENABLED=true
AI_WORLD_COMPARISON_TRACING_ENABLED=false
OTEL_SERVICE_NAME=catchhole-ai-worker
OTEL_EXPORTER_OTLP_TRACES_ENDPOINT=http://<MONITORING_PRIVATE_IPV4>:4318/v1/traces
```

세 종류별 값은 지정하지 않거나 비워 두면 공통 `AI_WORKER_TRACING_ENABLED`를 따른다.
명시한 `true`/`false`는 공통값보다 우선한다. 따라서 공통값만 `false`로 변경해도 명시적
`true` override는 꺼지지 않는다는 점에 주의한다. 환경 파일에는 같은 키를 중복 추가하지
않고 기존 값을 수정한다.

`OTEL_SERVICE_NAME`은 세 Worker에서 동일하게 유지한다. Trace resource의 `worker.kind`는
`analysis`, `character-comparison`, `world-comparison`으로 구분되고,
`deployment.environment.name=prod`, `service.instance.id`는 프로세스별 UUID다.
컨테이너를 다시 만들면 instance ID도 바뀐다. chatbot·검색 backfill은 이번 범위에 없다.

운영 활성화 전에 같은 SHA release 디렉터리의 배포 도구로 **읽기 전용 설정 검사**를 한다.
아래 명령은 Compose를 렌더링하고 설정을 검사할 뿐, image pull·readiness 호출·컨테이너
기동을 수행하지 않는다. 현재 배포 도구는 `worker.env`·`chatbot.env`·`retrieval.env`와
같은 release의 세 Compose 파일을 함께 검증한다.

```bash
python3 /opt/catchhole/releases/<FULL_COMMIT_SHA>/manage-ai-runtime.py validate \
  --directory /opt/catchhole \
  --release /opt/catchhole/releases/<FULL_COMMIT_SHA>
```

첫 canary에서 결과의 `tracingWorkerServices`에는 `ai-character-comparison-worker`
하나만 있어야 한다. 세 종류를 모두 켠 뒤에는 세 서비스 이름이 나온다.
검사는 `true|false`와 활성화 시 HTTP·RFC1918 IPv4·4318·`/v1/traces`를 요구하며,
인증 정보·query·fragment가 붙은 주소를 거절한다. 실패 출력에는 주소나 비밀값을 복사하지
않는다. 기존 자동배포의 preflight에도 같은 검사를 적용하므로 잘못된 설정은 pull/readiness/
재생성 전에 중단된다. `docker compose config --quiet`만으로 이 의미 검증을 대체하지 않는다.
사설 주소가 실제 Collector인지와 SG/네트워크 연결은 별도 확인 대상이다.

운영 전송 정책은 기존 Python runtime의 명시적 제한을 그대로 사용한다.

| 설정 | 초기 적용값과 의미 |
| --- | --- |
| Head sampling | `ParentBased(ALWAYS_ON)` — Worker 실행 root를 전량 기록하는 제한적 초기 검증 정책 |
| 완료 Span 큐 | 프로세스당 1,024개, 메모리 큐이며 디스크 재전송 보관소가 아님 |
| Batch | 최대 256개, 최대 1초마다 전송을 시도 |
| Export timeout | 5초, 실패해도 업무 HTTP/LLM 요청을 재실행하지 않음 |
| 종료 | 기존 claim 중단·최대 180초 drain·Worker close 이후 별도 thread에서 flush(5초 예산)·shutdown |
| Docker 종료 여유 | 기존 `stop_grace_period: 210s` 유지 |

SDK에 explicit 값을 전달하므로 `OTEL_BSP_*`만 추가해도 이 제한이 바뀌지는 않는다.
기본 프로세스 7개의 완료 Span 큐는 합계 최대 7,168개지만, 진행 중 Span·export 중 batch를
포함한 전체 메모리 상한은 아니다. Collector 장애·큐 포화·SIGKILL에는 Span이 유실될 수
있다. flush timeout도 프로세스 종료의 엄격한 시간 보장이나 Tempo 저장 보장이 아니다.
장기 작업의 마지막 root가 늦게 도착할 수 있으므로 Tail sampling을 자동으로 켜지 않는다.
먼저 실제 trace 길이·유입량·Collector/Tempo 메모리와 디스크를 관측하고 정책을 따로 결정한다.

실제 적용은 기존 immutable SHA 배포와 drain 절차를 사용한다. 설정을 `true`로 바꾼 뒤
실행 중 컨테이너를 재생성해야 반영되며 `worker.env` 수정만으로 즉시 바뀌지는 않는다.
처음에는 짧은 확인 시간 동안 승인된 소량의 작업으로 다음을 확인한다.

- 먼저 해당 이미지의 별도 합성/인증된 무과금 HTTP 확인으로 연결을 검사하고,
  승인된 기존 비교 작업 표본에서 캐릭터 비교 프로세스의 실제 Trace를 확인한다.
  합성 확인용 임시 프로세스는 Worker scheduler/claim을 시작하지 않으며 용량을 추가하지 않는다.
- `service.name=catchhole-ai-worker`, 환경 `prod`와 작업 종류가 맞는 Trace가 Tempo에 저장된다.
- 하나의 실행에서 Worker → Spring CLIENT/SERVER → JDBC의 부모 관계를 확인한다.
- LLM 대기·호출·정산 구간이 구분되고 기존 요청/오류/재시도 메트릭의 의미가 유지된다.
- Worker 일곱 개의 Prometheus 수집·작업 처리 상태와 Collector/Tempo 자원 상태가 정상이다.
  실제 작업이 없던 Worker 종류는 Trace가 아직 없을 수 있으므로 누락 여부와 구분한다.
- 원문·prompt·응답·S3 key·인증·lease·예외 원문이 Span에 없는지 표본을 검사한다.

운영 Trace 조회와 부모 관계를 확인하기 전에는 운영 적용 완료로 기록하지 않는다.
캐릭터 비교 한 프로세스의 확인은 전체 설정 추출 경로 검증이 아니다. 수집량·자원 예산이
허용되면 세계관 비교, 분석 Worker 5개 순으로 각 종류별 flag를 켜고, 회차·설정집 분석과
LLM 대기/정산 Trace를 추가 확인한다. 분석 종류별 flag를 켜면 기존 replica 5개가 함께
켜진다. 이후 전량 기록을 계속할지 또는 sampling을 적용할지는 별도로 결정한다.

중단할 때는 공통값과 **명시적으로 지정한 세 종류별 값 모두** `false`로 돌리고 같은
drain·재생성 절차를 적용한다. `validate`의 `tracingWorkerServices=[]`도 확인한다.
이는 이후 Span 생성을 끄며, 이미 Tempo에 저장한 데이터는 보존 정책에 따라 남는다.
전송을 끄기 위해 Worker 동시성이나 정상 분석 상태를 임의 변경하지 않는다.

로컬 설정·종료 경계 검증:

```bash
python -m pytest -q tests/test_ai_runtime_deploy.py tests/test_worker_tracing.py \
  tests/test_run_analysis_worker.py
```

외부 endpoint·운영 DB·유료 LLM은 호출하지 않는다. 합성 Trace 실험과 확인된 한계는
[worker-tracing.md](../docs/worker-tracing.md)를 참고한다.
