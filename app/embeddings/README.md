# embeddings

임베딩 생성과 pgvector 검색 흐름을 두는 패키지입니다.

현재 OpenAI Embeddings API Client와 모델·차원·버전 설정까지 구현되어 있습니다. 아직 설정 후보 추출/청크 저장 흐름에는 연결하지 않았으며, 설정 후보의 근거 표시는 `setting_candidates.source_chunk_id`, `evidence_spans`, `episode_chunks`의 원문 위치 정보를 우선 사용합니다.

다만 오류 리포트에 RAG를 붙이는 방향으로 결정되면, 사용자가 제보한 오류 내용이나 분석 결과의 의심 지점과 의미적으로 가까운 원문 chunk를 찾아야 합니다. 이 경우에는 단순 offset/quote 기반 근거 표시만으로는 부족하므로 `episode_chunks`에 대한 임베딩과 pgvector 검색 흐름이 필요합니다.

## 역할

후속 `NVM-141` 작업에서 다음 책임을 이 패키지에 둡니다.

- `episode_chunks.chunk_text`를 임베딩합니다.
- pgvector 저장/검색에 필요한 provider client를 감쌉니다.
- 사용자 질문, 오류 리포트 내용, 신규 문장과 관련된 기존 원문 chunk를 Top-K로 조회합니다.
- 작품, 회차, 캐릭터 같은 필터 조건을 검색에 적용할지 검토합니다.

## 현재 판단

근거 표시만을 위해서는 벡터 검색이 필수는 아닙니다.

예를 들어 설정 후보 상세 화면에서 "이 설정이 어느 원문에서 나왔는지"를 보여주는 경우에는 이미 저장된 `source_chunk_id`와 `evidence_spans[].quote`, 보정된 `start_offset` / `end_offset`을 사용할 수 있습니다. 이때 필요한 작업은 임베딩보다 quote를 실제 원문에서 찾아 offset을 보정하는 것입니다.

반면 챗봇, 유사 장면 검색, 신규 회차와 기존 원문 비교, 오류 리포트 RAG처럼 "의미적으로 비슷한 원문을 찾아야 하는 기능"에는 임베딩 검색이 필요합니다.

오류 리포트 RAG의 경우 사용자가 항상 정확한 원문 문장을 입력하지 않을 수 있습니다. 예를 들어 "캐릭터 설정이 앞 회차와 다른 것 같다" 또는 "이 분석 결과가 원문과 맞지 않는 것 같다"처럼 추상적인 상황이 들어오면, 해당 문장과 관련 있는 원문 chunk를 의미 검색으로 찾아 리포트 판단 근거에 포함해야 합니다.

따라서 임베딩은 오류 리포트가 참고할 원문 context를 검색하기 위한 기반 기능으로 우선 검토합니다.

## 현재 파일

- `client.py`: OpenAI Embeddings API 호출과 응답 차원 검증
- `responses.py`: batch embedding 응답 내부 값 객체

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

## 예상 파일

- `targets.py`: 어떤 원문 chunk를 임베딩할지 결정
- `search.py`: pgvector 기반 Top-K 검색
- `report_context.py`: 오류 리포트 RAG에 넘길 검색 context 구성

## 후속 논의

- 모든 `episode_chunks`를 임베딩할지, 특정 분석 대상만 임베딩할지 결정해야 합니다.
- 추출용 chunk 크기와 검색용 chunk 크기를 동일하게 가져갈지 분리할지 검토해야 합니다.
- 임베딩 모델, vector 차원, pgvector index 정책은 `NVM-141`에서 함께 결정합니다.
