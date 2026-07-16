# embeddings

청크 임베딩 생성·저장과 범용 pgvector 검색 흐름을 두는 패키지입니다.

현재 OpenAI Embeddings API Client와 모델·차원·버전 설정, 청크 임베딩 저장 서비스까지 구현되어 있습니다. Worker는 회차별 청크를 저장한 직후 `chunk_text` 목록을 한 번에 임베딩하고, 벡터와 모델·버전·생성 시각을 `episode_chunks`에 반영합니다.

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

NVM-141에 남은 범위:

- query text 임베딩과 cosine similarity 기반 Top-K Repository/Service
- 작품·회차 범위·제외 chunk ID 필터와 검색 결과 DTO
- 실제 pgvector 검색 테스트와 샘플 원고 품질 확인
- 기존 청크 backfill 및 재처리 방법

## 현재 판단

근거 표시만을 위해서는 벡터 검색이 필수는 아닙니다.

예를 들어 설정 후보 상세 화면에서 "이 설정이 어느 원문에서 나왔는지"를 보여주는 경우에는 이미 저장된 `source_chunk_id`와 `evidence_spans[].quote`, 보정된 `start_offset` / `end_offset`을 사용할 수 있습니다. 이때 필요한 작업은 임베딩보다 quote를 실제 원문에서 찾아 offset을 보정하는 것입니다.

반면 챗봇, 유사 장면 검색, 신규 회차와 기존 원문 비교, 오류 리포트 RAG처럼 "의미적으로 비슷한 원문을 찾아야 하는 기능"에는 임베딩 검색이 필요합니다.

오류 리포트 RAG의 경우 사용자가 항상 정확한 원문 문장을 입력하지 않을 수 있습니다. 예를 들어 "캐릭터 설정이 앞 회차와 다른 것 같다" 또는 "이 분석 결과가 원문과 맞지 않는 것 같다"처럼 추상적인 상황이 들어오면, 해당 문장과 관련 있는 원문 chunk를 의미 검색으로 찾아 리포트 판단 근거에 포함해야 합니다.

따라서 임베딩은 오류 리포트가 참고할 원문 context를 검색하기 위한 기반 기능으로 우선 검토합니다.

## 현재 파일

- `client.py`: OpenAI Embeddings API 호출과 응답 차원 검증
- `responses.py`: batch embedding 응답 내부 값 객체
- `service.py`: 청크 목록 임베딩과 `episode_chunks` 갱신 트랜잭션 관리

## 생성과 저장 흐름

```text
episode별 chunk 교체 저장
-> chunk_text 목록을 OpenAI Embeddings API에 한 번에 전달
-> 응답 index 기준으로 입력 순서 복원 및 차원 검증
-> embedding / embedding_model / embedding_version / embedded_at 갱신
-> commit
```

외부 API를 기다리는 동안 DB 트랜잭션을 점유하지 않도록 벡터 생성 후 세션을 엽니다. 현재 Worker는 API 호출이나 DB 갱신 중 발생한 예외를 임베딩 실패로 집계하고 설정 후보 추출을 계속합니다. 임베딩 갱신이 commit되지 않은 청크는 `NULL`로 남으며 후속 backfill 대상입니다.

현재는 임베딩 단계의 모든 예외를 같은 방식으로 처리합니다. 외부 API의 일시적 실패는 분석을 계속하되, 중복·누락된 chunk ID처럼 데이터 정합성을 나타내는 실패는 분석 작업에 전파하도록 예외 유형을 구분하는 작업이 남아 있습니다.

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

## 후속 구현 방향

- `search.py`에서 query text를 동일 모델로 임베딩하고 pgvector Top-K 검색을 호출합니다.
- Repository는 query vector와 작품·회차·제외 청크 조건만 받아 cosine distance를 계산합니다.
- 검색 결과는 chunk ID, episode ID와 번호, chunk index와 text, similarity를 내부 DTO로 반환합니다.
- HNSW와 `vector_cosine_ops` 인덱스는 Flyway V1에 구성되어 있으므로, 후속 PR에서는 실제 검색 쿼리와 품질을 검증합니다.
- 현재는 분석 작업에서 새로 만든 모든 `episode_chunks`를 같은 청킹 단위로 임베딩합니다. API 요청 크기를 고려한 backfill batch와 재처리 정책은 후속 작업에서 정합니다.
- NVM-143은 이 범용 검색 결과에 직접 source chunk, 기존 fact, 인접 문맥을 조합해 NVM-144에 넘길 검증 문맥을 만듭니다.
