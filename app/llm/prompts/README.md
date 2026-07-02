# prompts

LLM에 전달할 prompt 템플릿을 관리하는 패키지입니다.

## 관리 기준

- prompt 변경은 추출 품질과 저장 결과에 직접 영향을 줍니다.
- prompt는 코드처럼 버전 관리합니다.
- 변경 시 어떤 출력 형식이 바뀌는지 PR 본문에 남깁니다.
- DB 저장 schema와 맞물리는 출력 필드는 임의로 바꾸지 않습니다.

## 현재 파일

- `character_setting_extraction.md`
  - 웹소설 회차 청크에서 캐릭터 중심 설정 후보를 추출하기 위한 prompt입니다.
  - `setting_candidates` 저장 구조를 고려해 `source_chunk_id`, `entity_type`, `attribute_name`, `value_json`, `evidence_spans` 등을 반환하도록 요구합니다.
