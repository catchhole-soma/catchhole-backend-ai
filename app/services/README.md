# services

유스케이스 흐름을 조율하는 계층입니다.

Spring 기준으로는 Service 계층에 가깝습니다. Repository, storage, mapper, chunking 같은 하위 기능을 조합해 하나의 작업 흐름을 만듭니다.

## 역할

- 여러 Repository 또는 외부 storage 호출을 하나의 유스케이스로 묶습니다.
- 트랜잭션 경계를 관리합니다.
- 실패 시 rollback이 필요한 저장 흐름을 처리합니다.
- API route나 worker가 직접 세부 구현을 알지 않도록 중간 계층 역할을 합니다.

다음 책임은 Service에 넣지 않습니다.

- SQLAlchemy query 세부 작성
- S3 client 직접 호출 세부 구현
- LLM prompt 본문 작성
- FastAPI request/response schema 정의

## 현재 파일

- `analysis_job_service.py`
  - 분석 작업 상태 조회를 담당합니다.
  - 분석 실행은 Service가 직접 시작하지 않고, Worker가 Spring 내부 API를 통해 claim합니다.
- `episode_chunk_service.py`
  - 이미 원문 텍스트를 알고 있는 상태에서 회차 청킹 결과를 저장합니다.
  - `normalize_text -> split_into_chunks -> EpisodeChunkMapper -> EpisodeChunkRepository` 흐름을 조율합니다.
  - 같은 회차에 대해 청킹이 재실행될 때 기존 청크를 삭제하고 새 청크를 저장해 중복 생성을 막습니다.
- `episode_s3_chunking_service.py`
  - `Episode.content_s3_key`를 조회해 S3 원문을 읽습니다.
  - 읽은 원문을 `EpisodeChunkService`에 넘겨 청킹 저장을 수행합니다.

## 현재 연결 상태

- Spring claim payload 기반 Worker가 episode별로 `EpisodeS3ChunkingService`를 호출합니다.
- `analysis_jobs` 진행률, 완료, 실패는 DB를 직접 변경하지 않고 Spring 내부 API client를 통해 보고합니다.

## 후속 작업

- `upload_files.storage_url` 기준 원본 파일 로드는 필요한 시점에 별도 Service 또는 메서드로 분리합니다.
