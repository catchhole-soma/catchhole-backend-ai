# CatchHole 인공지능 작업 서버 운영 배포

이 문서는 Worker 서버용 Amazon EC2 인스턴스에서 설정 추출 Worker 5개, 캐릭터 비교 Worker 1개, 세계관 비교 Worker 1개를 실행하는 절차를 설명한다. API 서버, Caddy, Redis, PostgreSQL은 이 서버에서 실행하지 않는다.

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
```

`SPRING_INTERNAL_API_KEY`는 API 서버 `api.env`의 `INTERNAL_API_KEY`와 정확히 같은 값이어야 한다.

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

## 데이터베이스 연결 수 예산

| 실행 주체 | 프로세스 수 | 프로세스당 최대 연결 수 | 합계 |
| --- | ---: | ---: | ---: |
| Spring Backend | 1 | 10 | 10 |
| 설정 추출 Worker | 5 | 3 | 15 |
| 캐릭터 비교 Worker | 1 | 1 | 1 |
| 세계관 비교 Worker | 1 | 1 | 1 |
| 전체 |  |  | 27 |

SQLAlchemy의 `DATABASE_POOL_MAX_OVERFLOW=0`은 기본 연결 풀을 넘는 임시 연결 생성을 막는다. 전체 애플리케이션의 이론상 최대 연결 수는 27개로, NVM-315의 40개 이하 기준을 만족한다.

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
sudo -u ubuntu docker compose --env-file worker.env -f compose.worker.prod.yml stop -t 210 ai-worker ai-character-comparison-worker ai-world-comparison-worker
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
