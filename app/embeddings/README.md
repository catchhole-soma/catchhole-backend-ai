# embeddings

청크 임베딩 생성·저장과 범용 pgvector 검색 흐름을 두는 패키지입니다.

현재 OpenAI Embeddings API Client와 모델·차원·버전 설정, 청크 임베딩 저장 서비스, 범용 Top-K 검색 Service까지 구현되어 있습니다. Worker는 회차별 청크를 저장한 직후 `chunk_text` 목록을 한 번에 임베딩하고, 벡터와 모델·버전·생성 시각을 `episode_chunks`에 반영합니다.

오류 리포트는 RDB에서 직접 찾을 수 없는 과거 사건과 상태 변화의 원문 근거를 보완하기 위해 이 검색 기반을 사용합니다. NVM-141은 query text와 기본 검색 조건을 받아 유사 청크를 반환하는 범용 검색까지만 담당하며, `SettingCandidate`를 기준으로 검색어와 범위를 정하는 retrieval orchestration은 NVM-143에서 담당합니다.

## 역할

`NVM-141`에서 다음 책임을 이 패키지에 둡니다.

- `episode_chunks.chunk_text`를 임베딩합니다.
- pgvector 저장/검색에 필요한 provider client를 감쌉니다.
- 생성된 벡터와 모델·버전·생성 시각을 저장합니다.
- query text와 의미적으로 가까운 기존 원문 chunk를 Top-K로 조회합니다.
- 작품, 회차 범위, 제외할 chunk ID 같은 범용 검색 조건을 적용합니다.

설정 유형 분류, 캐릭터 매칭 상태별 검색 정책, `CharacterFact` 결합, query expansion, 인접 청크 확장과 rerank는 NVM-143의 책임입니다. 최종 충돌 판정은 NVM-144에서 수행합니다.

## 현재 구현 상태

이번 PR에서 완료된 범위:

- pgvector 의존성과 `EpisodeChunk.embedding` 모델 매핑
- 임베딩 모델·차원·버전 설정
- OpenAI Embeddings API 호출과 응답 순서·개수·차원 검증
- 회차별 신규 청크의 batch 임베딩 생성과 메타데이터 저장
- Worker 연결, 실패 개수 요약, 관련 단위 테스트와 문서화
- 검색 문장의 단건 임베딩과 cosine similarity 기반 Top-K 조회
- 작품·회차 범위·제외 청크·임베딩 모델 및 버전 필터

NVM-141에 남은 범위:

- 실제 pgvector 검색 테스트와 샘플 원고 품질 확인
- 기존 청크 backfill 및 재처리 방법

## 현재 판단

근거 표시만을 위해서는 벡터 검색이 필수는 아닙니다.

예를 들어 설정 후보 상세 화면에서 "이 설정이 어느 원문에서 나왔는지"를 보여주는 경우에는 이미 저장된 `source_chunk_id`와 `evidence_spans[].quote`, 보정된 `start_offset` / `end_offset`을 사용할 수 있습니다. 이때 필요한 작업은 임베딩보다 quote를 실제 원문에서 찾아 offset을 보정하는 것입니다.

반면 AI 채팅, 유사 장면 검색, 신규 회차와 기존 원문 비교, 오류 리포트 RAG처럼 "의미적으로 비슷한 원문을 찾아야 하는 기능"에는 임베딩 검색이 필요합니다.

## 사용 시나리오와 책임 경계

### 오류 리포트의 보완 근거 검색

오류 감지는 먼저 RDB의 구조화된 정보와 직접 연결된 원문 근거를 사용합니다. `SettingCandidate.source_chunk_id`, 관련 `CharacterFact`, 기존에 저장된 설정만으로 충돌을 설명할 수 있다면 벡터 검색은 필수가 아닙니다.

반면 RDB에 구조화되지 않은 중간 사건이 있거나 직접 근거만으로 판단이 애매한 경우에는 과거 원문을 의미 검색해 근거를 보완합니다. 예를 들어 스탯 감소가 단순 오류인지 저주·부상·디버프 때문인지, 나이 감소가 설정 충돌인지 회상·회귀 장면인지 확인하려면 관련 사건이 포함된 과거 청크를 찾아야 합니다.

```text
SettingCandidate와 직접 source chunk 확인
-> 관련 CharacterFact와 구조화된 DB 근거 조회
-> 근거가 없거나 판단이 애매하면 query와 회차 범위 생성
-> EpisodeChunkVectorSearchService로 과거 원문 Top-K 조회
-> 직접 근거, DB fact, 검색 청크를 함께 충돌 판정에 전달
```

이 fallback 여부와 검색 query를 결정하는 책임은 NVM-143의 retrieval orchestration에 있습니다. `EpisodeChunkVectorSearchService`는 근거가 충분한지 판단하지 않고, 전달받은 문장과 범위로 유사 청크를 반환하는 범용 기능만 담당합니다. 최종 충돌 판정은 NVM-144의 책임입니다.

### AI 채팅의 원문 문맥 검색

후속 AI 채팅 기능에서도 같은 범용 검색 기반을 재사용할 수 있습니다. 채팅 기능은 사용자의 질문을 query text로 사용하거나 검색용 문장으로 확장하고, 현재 작품과 공개 가능한 회차 범위를 지정해 관련 원문 Top-K를 가져온 뒤 답변 LLM의 문맥으로 제공할 수 있습니다.

```text
사용자 질문
-> 채팅 orchestration에서 검색 query와 작품·회차 범위 결정
-> EpisodeChunkVectorSearchService 호출
-> 관련 원문 Top-K를 답변 근거로 구성
-> 답변 생성
```

오류 리포트와 AI 채팅은 검색 이후의 정책은 다르지만, query embedding 생성과 `episode_chunks`의 pgvector Top-K 조회는 동일하게 재사용합니다.

## 현재 파일

- `client.py`: OpenAI Embeddings API 호출과 응답 차원 검증
- `exceptions.py`: 복구 가능한 provider 장애와 청크 데이터 정합성 오류 구분
- `responses.py`: batch embedding 응답 내부 값 객체
- `services/episode_chunk_embedding.py`: 청크 목록 임베딩과 `episode_chunks` 갱신 트랜잭션 관리
- `services/episode_chunk_vector_search.py`: 검색 문장 임베딩과 범용 pgvector Top-K 조회 연결

## 생성과 저장 흐름

```text
episode별 chunk 교체 저장
-> chunk_text 목록을 OpenAI Embeddings API에 한 번에 전달
-> 응답 index 기준으로 입력 순서 복원 및 차원 검증
-> embedding / embedding_model / embedding_version / embedded_at 갱신
-> commit
```

외부 API를 기다리는 동안 DB 트랜잭션을 점유하지 않도록 벡터 생성 후 세션을 엽니다. Worker는 timeout·연결 실패·HTTP 408/409/429/5xx를 `RecoverableEmbeddingProviderError`로 받아 임베딩 실패 개수를 기록하고 설정 후보 추출을 계속합니다. 이때 벡터가 저장되지 않은 청크는 `NULL`로 남으며 후속 backfill 대상입니다.

API Key 누락, HTTP 400/401/403, 응답 개수·index·차원 불일치, 중복·누락된 chunk ID, DB 연결·갱신 실패는 Worker가 삼키지 않습니다. 해당 예외는 `run_once()`까지 전파되어 Spring에 analysis job 실패로 보고됩니다. 중복 청크 ID는 불필요한 OpenAI 비용을 쓰지 않도록 Service에서 API 호출 전에 차단하고, Repository도 직접 호출될 때를 대비해 같은 정합성 검사를 유지합니다.

| 실패 유형 | 예시 | Worker 처리 |
| --- | --- | --- |
| 복구 가능한 provider 장애 | timeout, 연결 실패, 408, 409, 429, 5xx | 임베딩 실패 집계 후 설정 추출 계속 |
| 요청·인증 오류 | API Key 누락, 400, 401, 403 | analysis job 실패 |
| 응답 계약 오류 | 벡터 개수·index·차원 불일치 | analysis job 실패 |
| 데이터 정합성 오류 | 중복 chunk ID, 저장 대상 chunk 누락 | analysis job 실패 |
| DB 오류 | 연결·조회·UPDATE·commit 실패 | rollback 후 analysis job 실패 |

## OpenAI Embeddings API 응답 예시

아래는 [OpenAI 공식 Embeddings API 응답 형식](https://developers.openai.com/api/reference/resources/embeddings/methods/create)을 이 프로젝트의 모델 설정에 맞춰 축약한 예시입니다. 실제 `embedding` 배열에는 `episode_chunks.embedding` 컬럼과 동일한 1536개의 실수가 들어갑니다.

```json
{
  "object": "list",
  "data": [
    {
      "object": "embedding",
      "index": 0,
      "embedding": [0.0023064255, -0.009327292, 0.015797377]
    }
  ],
  "model": "text-embedding-3-small",
  "usage": {
    "prompt_tokens": 8,
    "total_tokens": 8
  }
}
```

- `data[].index`: 요청한 `input` 배열에서 해당 벡터가 대응하는 위치
- `data[].embedding`: `text-embedding-3-small`이 생성한 1536차원 벡터
- `model`: 실제 응답에 사용된 임베딩 모델
- `usage.prompt_tokens`: 임베딩 입력에 사용된 토큰 수

`client.py`는 `data[].index`를 기준으로 벡터를 입력 순서대로 정렬하고, 응답 개수와 벡터 차원을 검증합니다. 이후 벡터와 모델명, 토큰 수, 원본 응답을 `EmbeddingBatchResponse`로 변환합니다.

## 검색 흐름

```text
query text와 작품·회차·제외 청크 조건 입력
-> 저장된 청크와 동일한 모델로 query embedding 생성
-> EpisodeChunkRepository의 pgvector cosine distance 검색 호출
-> distance 오름차순 Top-K를 similarity와 함께 반환
```

검색 문장을 임베딩하는 동안에는 DB 세션을 열지 않습니다. Repository는 같은 embedding model·version으로 생성된 청크만 비교하며, 결과는 chunk ID, episode ID와 번호, chunk index와 text, similarity를 포함합니다.

## 실제 PostgreSQL 통합 테스트

Repository 단위 테스트는 SQL 구성과 결과 변환을 빠르게 확인하고, `tests/integration/test_episode_chunk_vector_search_repository.py`는 실제 PostgreSQL과 pgvector가 벡터 거리·정렬·필터를 처리하는지 확인합니다.

통합 테스트는 `PGVECTOR_TEST_DATABASE_URL`이 없으면 자동으로 건너뜁니다. 로컬에서는 Spring 저장소와 같은 pgvector 이미지를 실행한 뒤 다음처럼 검증합니다.

```bash
docker compose -f ../catchhole-backend-java/compose.yaml up -d postgres

PGVECTOR_TEST_DATABASE_URL=postgresql+psycopg://myuser:secret@localhost:15432/mydatabase \
  .venv/bin/pytest -m integration -q
```

테스트는 현재 연결 안에 `episodes`, `episode_chunks` 임시 테이블과 HNSW cosine 인덱스를 만들고 종료 시 transaction rollback으로 모두 제거합니다. 따라서 같은 DB의 기존 테이블과 데이터는 수정하지 않습니다.

## 후속 구현 방향

- HNSW와 `vector_cosine_ops` 인덱스는 Flyway V1에 구성되어 있고 실제 pgvector 검색 통합 테스트도 연결되었습니다. 후속 작업에서는 샘플 원고 검색 품질을 검증합니다.
- 현재는 분석 작업에서 새로 만든 모든 `episode_chunks`를 같은 청킹 단위로 임베딩합니다. API 요청 크기를 고려한 backfill batch와 재처리 정책은 후속 작업에서 정합니다.
- NVM-143은 이 범용 검색 결과에 직접 source chunk, 기존 fact, 인접 문맥을 조합해 NVM-144에 넘길 검증 문맥을 만듭니다.
