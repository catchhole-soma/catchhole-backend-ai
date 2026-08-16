# prompts

LLM에 전달할 prompt 템플릿을 관리하는 패키지입니다.

## 관리 기준

- prompt 변경은 추출 품질과 저장 결과에 직접 영향을 줍니다.
- prompt는 코드처럼 버전 관리합니다.
- 변경 시 어떤 출력 형식이 바뀌는지 PR 본문에 남깁니다.
- DB 저장 schema와 맞물리는 출력 필드는 임의로 바꾸지 않습니다.

## 현재 파일

- `character_setting_extraction.md`
  - 웹소설 회차 청크에서 캐릭터 중심 설정 후보와 명시적 신규 캐릭터 발견 후보를 추출하기 위한 prompt입니다.
  - `setting_candidates` 저장 구조를 고려해 `candidate_kind`, `entity_type`, `attribute_name`, `value_json`, `evidence_spans` 등을 반환하도록 요구합니다.
  - `source_chunk_id`는 LLM 출력에 맡기지 않고 응답 파싱 후 현재 입력 `EpisodeChunk.id`로 주입합니다.
- `character_subject_resolution.md`
  - 이미 추출된 설정 후보 중 `entity_name`이 구체적이지 않은 후보의 주체만 해소하기 위한 prompt입니다.
  - 설정 후보를 다시 추출하지 않고, current chunk 기준으로 묶인 후보들의 `resolved_entity_name`만 반환하도록 요구합니다.
- `character_fact_comparison.md`
  - 매칭된 캐릭터 설정 후보 한 건과 현재 `WorkCharacter` snapshot을 비교해 ADD/UPDATE/MERGE/REMOVE/HISTORY_ONLY/EXCLUDE/REVIEW_REQUIRED를 제안합니다.
  - 같은 batch에서 앞서 나온 동일 canonical slot 후보를 미확정 시간순 문맥으로 함께 받아 상대 변화량을 최종값으로 오인하지 않게 합니다.
  - DB 식별자 대신 요청 안에서만 유효한 `P*` 참조를 사용하며, 원문·후보·snapshot 안의 명령은 소설 데이터일 뿐 지시가 아니라고 명시합니다.
  - 회상·가정은 현재 snapshot을 바꾸지 않으며, STATUS 제거는 명시적인 현재 결과가 있을 때만 제안합니다. 제거 제안도 원본 CharacterFact 이력을 삭제하지 않습니다.
- `world_setting_extraction.md`
  - 회차 청크에서 일시적 사건·현재 상태를 제외하고 지속 가능한 세계관 속성을 한 행 단위로 추출합니다.
  - 7개 category, 원문 evidence quote, 고정 confidence 단계와 문자열 속성값을 요구합니다.
- `world_setting_subject_resolution.md`
  - 같은 category의 기존 대상명 중 후보와 의미상 같은 대상을 최대 3개까지 고릅니다.
  - UUID 대신 Worker가 만든 `S*` 참조만 입력·출력에 사용합니다.
- `world_setting_comparison.md`
  - 후보 속성과 최대 3개 기존 대상의 현재 properties를 비교해 ADD/UPDATE/MERGE/EXCLUDE를 제안합니다.
  - UUID/version 대신 `T*` 참조를 사용하고, UPDATE/MERGE의 실제 속성명과 최종 문자열을 반환합니다.

## 설정 후보 출력 계약

`character_setting_extraction.md`는 Spring의 설정 확정 흐름과 맞도록 다음 계약을 둡니다.

- LLM의 `attribute_name`은 먼저 `SettingCandidate.attributeName`에 후보 key로 저장됩니다.
- `candidate_kind=SETTING`은 기존 설정 payload를 사용하고, `candidate_kind=CHARACTER_DISCOVERY`는 명시적 이름의 존재만 나타내므로 `attribute_name`, `attribute_value`, `value_type`, `value_json`을 모두 null로 둡니다.
- user prompt의 `known_character_names`에 이미 있는 이름은 발견 후보로 반환하지 않습니다. 같은 신규 이름은 청크당 가장 명확한 근거 하나만 반환합니다.
- 이름과 지속 속성이 한 문장에 함께 있으면 같은 `entity_name`의 발견 후보와 설정 후보를 각각 반환할 수 있습니다. 예를 들어 `케닉의 넷째 아들 세룸`은 `세룸` 발견과 활성 schema에 맞는 가족 관계 설정을 함께 만들 수 있습니다.
- 같은 청크의 동일 캐릭터·`attribute_name`·`value_type`·`value_json` 설정은 근거 문장마다 반복하지 않고 가장 직접적인 근거 하나만 반환합니다. 표시용 `attribute_value`만 다르면 중복으로 보고, 실제 `value_json`이 다르면 값 변화 가능성을 보존합니다.
- 현재 적용 중인 상태뿐 아니라 완화·종료·전환된 현재 결과도 STATUS 후보로 보존합니다. 특정 행동을 고정 기준으로 삼지 않고 능력·증상·행동·적용 효과의 변화를 종합하되, 치료 수단만 있고 결과가 없으면 회복 완료로 추출하지 않습니다.
- Backend confirm에서 exact/alias match는 canonical `schemaKey`를, pattern match는 구체
  `SettingCandidate.attributeName`을 `CharacterFact.factKey`로 확정합니다.
- `raw_entity_mention`은 원문에 실제 등장한 표현이고, `entity_name`은 원문 맥락에서 정리한 후보 캐릭터명입니다.
- 나이/레벨은 `age`, `level` 고정 key를 사용합니다.
- 여러 항목이 공존하는 값은 제공된 schema의 `attributePattern`에 따라
  `stats.<스탯명>`, `skill.<스킬명>`, `item.<아이템명>`, `status.<상태명>`처럼 구체 key를 포함합니다.
- user prompt의 `character_setting_schemas`는 `schemaKey`, `displayName`, `attributePattern`, `aliases`, `valueType`만 포함합니다.
- 고정 schema와 명확히 대응하면 canonical `schemaKey`와 schema `valueType`을 사용하고, 동적 schema는 `attributePattern`의 `*`를 구체 명칭으로 치환합니다.
- 시간·사건·타임라인 정보와 제공된 schema에 대응하지 않는 설정은 후보에서 제외합니다. 가까운 schema로 fuzzy 정규화하거나 새 key를 만들지 않습니다.
- `NUMBER`와 `BOOLEAN` 후보의 저장 `attribute_value`는 `value_json.value`에서 결정합니다.
  NUMBER는 숫자 문자열, BOOLEAN은 소문자 `true`/`false`이며 LLM의 원래 표시 문구는
  `raw_ai_result_json`에 보존합니다. 그 밖의 타입은 기존 표시 summary를 유지합니다.
- `value_json`은 실제 값의 source of truth입니다. NUMBER/BOOLEAN의 `value`가 선언 타입과
  다르면 추출 응답을 거절하고 재시도합니다.
- `source_chunk_id`는 prompt 출력 필드가 아니며 Python Worker가 현재 입력 chunk ID로 결정합니다.
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

## 세계관 prompt 출력 계약

- 세계관 후보 한 건은 `category + subject_name + setting_name + extracted_value`로 표현되는 속성 하나입니다.
- chunk별 추출 뒤 같은 `category + subject_name + setting_name` 후보는 게시 전에 한 건으로 통합합니다. 2차 비교 입력의 `extracted_values`는 통합 전 값 목록이며, 모델은 `SINGLE/MERGED/CONFLICT`를 판정합니다. `MERGED`는 모든 양립 가능한 정보를 보존한 자연스러운 `proposed_value` 하나를 반환하고, `CONFLICT`는 임의 절충 없이 입력값 전체를 그대로 반환합니다.
- 통합 후보의 `evidence_spans`는 각 1차 후보의 실제 quote·offset 합집합입니다. 2차 비교는 이 근거를 수정하거나 새로 만들지 않습니다.
- 추출 근거 quote는 원문 그대로 복사하며 offset은 Python mapper가 현재 chunk에서 다시 계산합니다.
- 대상 탐색과 상세 비교 prompt에는 Backend UUID와 version을 넣지 않습니다. LLM은 입력에 있는 `S*`/`T*` ref만 반환합니다.
- 대상 탐색은 같은 대상일 가능성이 없으면 빈 목록을 반환하고, 단순 연관성만으로 선택하지 않습니다.
- ADD/EXCLUDE는 추출 설정명과 값을 보존합니다. UPDATE/MERGE는 선택한 target에 실제 존재하는 속성명을 그대로 사용합니다.
- 기존 속성과 중복되어 EXCLUDE할 때는 해당 `T*` 참조와 실제 속성명을 함께 반환해 Backend가 비교 당시 기존값을 보존합니다. 특정 기존 속성과 비교하지 않은 일시적 사건 등의 제외만 매칭 속성명을 비웁니다.
- MERGE의 `proposed_value`는 기존·신규 정보를 모두 보존하되 중복을 제거한 최종 문자열 한 개입니다.
- Python schema가 ref와 operation별 필드를 검증하고, Backend가 실제 대상 ID·현재 version·속성 존재 여부를 다시 검증합니다.
- 정상 응답의 null/placeholder 결과는 Python이 원래 후보를 `미상`으로 보존해 `AMBIGUOUS`로 저장하며, candidate ID 누락·중복·추가는 기술적 계약 오류로 처리합니다.
- 규칙 기반 문자열 검색으로 주체를 확정하지 않고, 문맥상 확실할 때만 이름을 반환하도록 요구합니다.
