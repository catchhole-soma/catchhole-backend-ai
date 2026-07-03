# FastAPI Role Review

Python AI Worker에서 FastAPI를 계속 유지할지, health check 수준으로 축소할지, 제거할지 검토하기 위한 문서입니다.

현재 분석 실행의 핵심 경로는 FastAPI endpoint가 아니라 Worker polling입니다. 따라서 FastAPI는 분석 실행 경로가 아니라 보조 HTTP 인터페이스로 봅니다.

## 현재 실행 경로

```text
Spring 분석 job 생성
-> Python Worker가 Spring 내부 claim API polling
-> S3 원문 조회
-> 청킹
-> LLM 설정 후보 추출
-> setting_candidates 저장
-> Spring 내부 API로 progress / complete / fail 보고
```

이 흐름에서 Spring이 Python의 `POST /analysis` 같은 API를 호출하지 않습니다. Python이 Spring 내부 API를 호출해 작업을 가져오고 상태를 보고합니다.

## 현재 FastAPI 흔적

| 위치 | 역할 | 현재 판단 |
| --- | --- | --- |
| `app/main.py` | FastAPI app 생성 | health/status API를 띄울 때만 필요 |
| `app/api/routes/health.py` | health check | 배포 환경의 HTTP health check가 필요하면 유지 가능 |
| `app/api/routes/analysis_jobs.py` | 분석 job 상태 조회 API | Spring이 상태 source라면 책임 중복 가능성이 있음 |
| `app/exceptions/*` | FastAPI 예외 응답 변환 | FastAPI endpoint를 유지하는 동안만 필요 |
| Swagger `/docs`, `/redoc` | FastAPI 문서 UI | Worker-only 구조에서는 핵심 문서가 아님 |

## 유지할 이유

- 배포 환경에서 HTTP health check endpoint가 필요할 수 있습니다.
- 운영용 내부 상태 확인 API를 나중에 붙일 수 있습니다.
- 로컬에서 서버가 살아있는지 확인하기 쉽습니다.
- FastAPI를 완전히 제거하기 전까지 기존 테스트와 구조를 안정적으로 유지할 수 있습니다.

## 축소 또는 제거를 검토하는 이유

- 현재 분석 실행은 FastAPI endpoint를 통하지 않습니다.
- 처음 보는 사람이 "Spring이 Python API를 호출해 분석을 시작한다"고 오해할 수 있습니다.
- 분석 상태의 source는 Spring인데, Python에 상태 조회 API가 있으면 책임이 겹칠 수 있습니다.
- Worker-only 배포라면 `scripts/run_analysis_worker.py`가 실제 entrypoint이고 FastAPI app은 불필요할 수 있습니다.

## 선택지

### 선택지 1. health check만 유지

FastAPI를 최소 HTTP health check 용도로만 남깁니다.

```text
GET /api/v1/health
```

장점:

- 배포 health check를 쉽게 구성할 수 있습니다.
- 분석 실행 경로와 혼동되는 API를 줄일 수 있습니다.

주의점:

- Swagger UI의 의미가 거의 사라집니다.
- Python API 서버 프로세스와 Worker 프로세스를 함께 띄울지 분리할지 결정해야 합니다.

### 선택지 2. FastAPI 제거

Python repo를 완전한 Worker process로 정리합니다.

장점:

- 실행 경로가 단순해집니다.
- Spring 중심 API 구조가 더 명확해집니다.

주의점:

- HTTP health check가 필요한 배포 환경에서는 대체 방법이 필요합니다.
- 기존 FastAPI route/test/exception 구조를 함께 정리해야 합니다.

### 선택지 3. 내부 운영 API로 유지

FastAPI를 분석 실행 API가 아니라 내부 운영 API로 제한합니다.

예시:

- health check
- worker 설정 확인
- 현재 process 상태 확인

장점:

- 운영 관측성을 조금 더 확보할 수 있습니다.

주의점:

- 운영 API 범위가 커지면 Spring과 책임이 겹칠 수 있습니다.
- 보안 정책과 내부 접근 제한이 필요합니다.

## 임시 정책

최종 결정 전까지는 다음 기준을 따릅니다.

- 새 분석 실행 API를 FastAPI에 추가하지 않습니다.
- 분석 job 생성, 조회, 사용자-facing API는 Spring이 담당합니다.
- Python의 실제 분석 실행 entrypoint는 `scripts/run_analysis_worker.py`입니다.
- FastAPI route는 health check 또는 임시 상태 확인 수준으로만 유지합니다.
- `analysis_jobs` 상태의 원천은 Spring으로 둡니다.

## 정리 후보

후속 이슈에서 다음 항목을 검토합니다.

- `GET /api/v1/analysis-jobs/{analysis_job_id}/status`가 Spring 책임과 겹치는지 확인
- Swagger/Redoc 안내를 README에서 얼마나 강조할지 조정
- FastAPI app과 Worker runner를 같은 배포 단위로 둘지 분리할지 결정
- health check만 남길 경우 `app/exceptions`와 `app/schemas/analysis.py` 범위를 축소할 수 있는지 검토

## 결론

현재 기준에서는 FastAPI가 분석 실행에 필수는 아닙니다.

다만 배포 health check와 향후 운영용 endpoint 가능성이 있으므로 바로 제거하기보다, 우선 "보조 HTTP 인터페이스"로 명확히 문서화하고 다음 이슈에서 축소 또는 제거 여부를 결정합니다.
