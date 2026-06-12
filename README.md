# catchhole-backend-ai

CatchHole의 원고 분석, 청킹, 설정 후보 추출, 근거 저장 흐름을 담당하는 Python AI 서버입니다.

## 역할

- Spring 서버가 생성한 `analysis_jobs`를 기준으로 분석 작업을 수행합니다.
- S3에 저장된 회차 원문을 읽어 회차/청크 단위로 분리합니다.
- AI가 추출한 설정 후보와 근거 정보를 PostgreSQL에 저장할 예정입니다.
- Spring 서버는 사용자 인증, 작품 소유권 검증, API 응답, 사용자의 후보 확정 흐름을 담당합니다.

## 현재 범위

아직 실제 DB/S3/LLM 연동은 붙이지 않았고, 서버 구조와 분석 잡 실행 계약을 먼저 잡았습니다.

```text
Spring API
  -> analysis_jobs 생성
  -> Python worker 실행 또는 큐 발행
  -> Python worker가 S3 원문 조회
  -> chunk / setting_candidates / evidence 저장
  -> Spring API가 진행률과 결과를 조회
```

## 로컬 실행

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
uvicorn app.main:app --reload
```

헬스 체크:

```bash
curl http://localhost:8000/api/v1/health
```

## 환경 변수

`.env.example`을 참고해 `.env`를 생성합니다.

- `DATABASE_URL`: Spring 서버와 공유하는 PostgreSQL 연결 문자열
- `AWS_S3_BUCKET`: 회차 원문과 업로드 파일이 저장되는 S3 버킷
- `AWS_SQS_QUEUE_URL`: 분석 잡 큐를 붙일 경우 사용할 SQS URL
- `LLM_API_KEY`: 설정 추출/검증에 사용할 LLM API 키

## API 초안

- `GET /api/v1/health`
- `POST /api/v1/analysis-jobs/{analysis_job_id}/run`
- `GET /api/v1/analysis-jobs/{analysis_job_id}/status`
