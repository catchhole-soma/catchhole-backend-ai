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
- `character_subject_resolution.md`
  - 이미 추출된 설정 후보 중 지칭어/placeholder 주체만 해소하기 위한 prompt입니다.
  - 설정 후보를 다시 추출하지 않고, current chunk 기준으로 묶인 후보들의 `resolved_entity_name`만 반환하도록 요구합니다.

## 설정 후보 출력 계약

`character_setting_extraction.md`는 Spring의 설정 확정 흐름과 맞도록 다음 계약을 둡니다.

- `attribute_name`은 백엔드의 `factKey`로 저장됩니다.
- `raw_entity_mention`은 원문에 실제 등장한 표현이고, `entity_name`은 원문 맥락에서 정리한 후보 캐릭터명입니다.
- 나이/레벨은 `age`, `level` 고정 key를 사용합니다.
- 여러 항목이 공존하는 값은 `stats.<스탯명>`, `skills.<스킬명>`, `items.<아이템명>`, `status.<상태명>`, `time.<시간 또는 사건명>`처럼 구체 key를 포함합니다.
- `attribute_value`는 목록/검토 화면 표시용 summary이며, 로직 판단 기준으로 사용하지 않습니다.
- `value_json`은 실제 값의 source of truth입니다. 나이/레벨은 `{"value": number}` 형태를 우선 사용합니다.
- `evidence_spans[].quote`는 위치 보정 기준이므로 원문 일부를 요약/의역하지 않고 그대로 복사해야 합니다.
- `evidence_spans[].start_offset`, `end_offset`은 LLM이 계산하지 않고 Python Worker가 quote 검색으로 보정합니다.

## subject resolution 출력 계약

`character_subject_resolution.md`는 설정 추출 결과를 다시 만들지 않고, 이미 추출된 후보의 주체만 해소합니다.

- 입력은 previous/current/next chunk와 current chunk에서 나온 fallback 대상 후보 목록입니다.
- 출력은 입력 candidates의 `candidate_id`별 resolution만 포함합니다.
- 문맥상 확실하면 `resolved_entity_name`을 반환합니다.
- 주체를 알 수 없거나 둘 이상의 후보가 가능하면 `resolved_entity_name`은 null로 둡니다.
- 모든 candidate_id는 응답에 포함해야 하며, 애매한 후보도 생략하지 않고 null로 반환합니다.
- `resolved_entity_name`에는 `미상`, `불명`, `unknown`, `나`, `그`, `그녀`, `주인공` 같은 placeholder/지칭어를 넣지 않습니다.
- `MATCHED`, `UNRESOLVED`, `AMBIGUOUS` 같은 최종 매칭 상태는 Python의 `character_name_resolver`가 계산합니다.
- 규칙 기반 문자열 검색으로 주체를 확정하지 않고, 문맥상 확실할 때만 이름을 반환하도록 요구합니다.
