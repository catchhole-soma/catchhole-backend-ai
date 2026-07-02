# embeddings

임베딩 생성과 pgvector 검색 흐름을 두는 패키지입니다.

현재 MVP의 설정 후보 추출/저장 흐름에서는 아직 임베딩을 수행하지 않습니다. 설정 후보의 근거 표시는 `setting_candidates.source_chunk_id`, `evidence_spans`, `episode_chunks`의 원문 위치 정보를 우선 사용합니다.

## 역할

후속 `NVM-141` 작업에서 다음 책임을 이 패키지에 둡니다.

- `episode_chunks.chunk_text`를 임베딩합니다.
- pgvector 저장/검색에 필요한 provider client를 감쌉니다.
- 사용자 질문 또는 신규 문장과 관련된 기존 원문 chunk를 Top-K로 조회합니다.
- 작품, 회차, 캐릭터 같은 필터 조건을 검색에 적용할지 검토합니다.

## 현재 판단

근거 표시만을 위해서는 벡터 검색이 필수는 아닙니다.

예를 들어 설정 후보 상세 화면에서 "이 설정이 어느 원문에서 나왔는지"를 보여주는 경우에는 이미 저장된 `source_chunk_id`와 `evidence_spans[].quote`, 보정된 `start_offset` / `end_offset`을 사용할 수 있습니다. 이때 필요한 작업은 임베딩보다 quote를 실제 원문에서 찾아 offset을 보정하는 것입니다.

반면 챗봇, 유사 장면 검색, 신규 회차와 기존 원문 비교처럼 "의미적으로 비슷한 원문을 찾아야 하는 기능"에는 임베딩 검색이 필요합니다.

## 예상 파일

- `client.py`: embedding provider 호출 래퍼
- `targets.py`: 어떤 원문 chunk를 임베딩할지 결정
- `search.py`: pgvector 기반 Top-K 검색

## 후속 논의

- 모든 `episode_chunks`를 임베딩할지, 특정 분석 대상만 임베딩할지 결정해야 합니다.
- 추출용 chunk 크기와 검색용 chunk 크기를 동일하게 가져갈지 분리할지 검토해야 합니다.
- 임베딩 모델, vector 차원, pgvector index 정책은 `NVM-141`에서 함께 결정합니다.
