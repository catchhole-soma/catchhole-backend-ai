# embeddings

임베딩 생성과 pgvector 검색 흐름을 두는 패키지입니다.

현재 MVP의 설정 후보 추출/저장 흐름에서는 아직 임베딩을 수행하지 않습니다. 설정 후보의 근거 표시는 `setting_candidates.source_chunk_id`, `evidence_spans`, `episode_chunks`의 원문 위치 정보를 우선 사용합니다.

다만 오류 리포트에 RAG를 붙이는 방향으로 결정되면, 사용자가 제보한 오류 내용이나 분석 결과의 의심 지점과 의미적으로 가까운 원문 chunk를 찾아야 합니다. 이 경우에는 단순 offset/quote 기반 근거 표시만으로는 부족하므로 `episode_chunks`에 대한 임베딩과 pgvector 검색 흐름이 필요합니다.

## 역할

후속 `NVM-141` 작업에서 다음 책임을 이 패키지에 둡니다.

- `episode_chunks.chunk_text`를 임베딩합니다.
- pgvector 저장/검색에 필요한 provider client를 감쌉니다.
- 사용자 질문, 오류 리포트 내용, 신규 문장과 관련된 기존 원문 chunk를 Top-K로 조회합니다.
- 작품, 회차, 캐릭터 같은 필터 조건을 검색에 적용할지 검토합니다.

## 현재 판단

근거 표시만을 위해서는 벡터 검색이 필수는 아닙니다.

예를 들어 설정 후보 상세 화면에서 "이 설정이 어느 원문에서 나왔는지"를 보여주는 경우에는 이미 저장된 `source_chunk_id`와 `evidence_spans.quote`를 사용할 수 있습니다. 이때 필요한 작업은 임베딩보다 `evidence_quote`를 실제 원문에서 찾아 offset을 보정하는 것입니다.

반면 챗봇, 유사 장면 검색, 신규 회차와 기존 원문 비교, 오류 리포트 RAG처럼 "의미적으로 비슷한 원문을 찾아야 하는 기능"에는 임베딩 검색이 필요합니다.

오류 리포트 RAG의 경우 사용자가 항상 정확한 원문 문장을 입력하지 않을 수 있습니다. 예를 들어 "캐릭터 설정이 앞 회차와 다른 것 같다" 또는 "이 분석 결과가 원문과 맞지 않는 것 같다"처럼 추상적인 상황이 들어오면, 해당 문장과 관련 있는 원문 chunk를 의미 검색으로 찾아 리포트 판단 근거에 포함해야 합니다.

따라서 임베딩은 오류 리포트가 참고할 원문 context를 검색하기 위한 기반 기능으로 우선 검토합니다.

## 예상 파일

- `client.py`: embedding provider 호출 래퍼
- `targets.py`: 어떤 원문 chunk를 임베딩할지 결정
- `search.py`: pgvector 기반 Top-K 검색
- `report_context.py`: 오류 리포트 RAG에 넘길 검색 context 구성

## 후속 논의

- 모든 `episode_chunks`를 임베딩할지, 특정 분석 대상만 임베딩할지 결정해야 합니다.
- 추출용 chunk 크기와 검색용 chunk 크기를 동일하게 가져갈지 분리할지 검토해야 합니다.
- 임베딩 모델, vector 차원, pgvector index 정책은 `NVM-141`에서 함께 결정합니다.
