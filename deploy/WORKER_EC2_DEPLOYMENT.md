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
DATABASE_URL=postgresql+psycopg://catchhole_admin:replace-with-url-encoded-password@replace-with-rds-endpoint:5432/catchhole?sslmode=require
DATABASE_POOL_SIZE=3
DATABASE_POOL_MAX_OVERFLOW=0
```

사용자 이름 또는 비밀번호에 `@`, `:`, `/`, `?`, `#`, `%` 같은 예약 문자가 있으면 URL 백분율 인코딩을 적용해야 한다. 원래 비밀번호를 바꾸는 것이 아니라 연결 문자열에 넣는 표현만 인코딩한다.

Worker 서버에서 PostgreSQL 클라이언트로 연결을 확인한다. 아래 명령의 엔드포인트와 사용자 이름은 실제 값으로 바꾸며, 비밀번호는 프롬프트에서 입력한다.

```bash
psql "host=replace-with-rds-endpoint port=5432 dbname=catchhole user=catchhole_admin sslmode=require" -W
```

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
