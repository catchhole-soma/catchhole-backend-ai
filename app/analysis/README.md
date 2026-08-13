# analysis

AI 분석 유스케이스와 분석 판단 로직을 두는 패키지입니다.

Spring 기준으로는 여러 하위 기능을 조합해 도메인 분석 결과를 만드는 Service/Domain Service에 가깝습니다.

## 역할

- 원문 청크를 입력으로 받아 캐릭터 설정·신규 캐릭터 발견 후보와 지속 가능한 세계관 속성 후보를 추출합니다.
- LLM 응답 JSON을 Python 내부 검증 schema로 확인합니다.
- 추출 결과를 `setting_candidates` 저장 구조에 맞는 중간 결과로 정리합니다.
- 근거 문장을 원문에서 다시 찾아 회차 전체 기준 위치를 계산합니다.
- 추출 후보의 캐릭터명 표현을 기존 캐릭터 목록과 비교해 매칭 상태를 계산합니다.
- 매칭된 캐릭터 후보와 현재 snapshot을 비교해 현재 화면 반영·검토·제거 제안을 생성합니다.
- 세계관 후보와 같은 category의 기존 대상을 좁히고 ADD/UPDATE/MERGE/EXCLUDE 제안을 생성합니다.
- NVM-143의 검증 근거 수집과 NVM-144의 충돌 판정은 후속 단계에서 연결합니다.

다음 책임은 Analysis에 넣지 않습니다.

- OpenAI HTTP 호출 세부 구현
- S3 원문 조회
- SQLAlchemy query 세부 작성
- Spring 내부 Worker API 호출

## 현재 파일

- `setting_extractor.py`
  - 청크 하나를 LLM에 보내 캐릭터 설정 후보와 이름 발견 후보를 추출합니다.
  - Spring claim DTO를 Worker가 변환한 immutable schema hint를 user prompt에 포함합니다.
  - claim의 기존 캐릭터 대표 이름을 user prompt에 포함해 이미 등록된 이름의 발견 후보를 만들지 않게 합니다. Backend 내부 매칭용 ID는 prompt에 포함하지 않습니다.
  - prompt 로드, user prompt 구성, JSON 파싱, 결정적 `source_chunk_id` 주입, schema 검증, 검증 실패 재시도를 담당합니다.
- `evidence_span_resolver.py`
  - LLM이 반환한 `evidence_spans[].quote`를 청크 원문에서 다시 찾아 offset을 보정합니다.
  - exact match를 우선 사용하고, 실패하면 공백/줄바꿈 정규화 기반 검색을 시도합니다.
  - quote를 찾지 못하면 잘못된 위치를 저장하지 않도록 offset을 null로 유지합니다.
- `character_name_resolver.py`
  - `KnownCharacter` 목록과 추출 후보의 `raw_entity_mention`, `entity_name`을 비교합니다.
  - 기존 캐릭터 하나와 확실히 연결되면 `MATCHED`, 후보가 없으면 `UNRESOLVED`, 대명사/복수 후보처럼 위험하면 `AMBIGUOUS`를 반환합니다.
- `character_subject_resolver.py`
  - `entity_name`이 비어 있거나 `미상`/지칭어 같은 구체적이지 않은 값인 후보를 `raw_entity_mention`의 형태와 관계없이 LLM으로 한 번 더 해소합니다.
  - 같은 current chunk에서 나온 fallback 대상 후보를 묶어 previous/current/next chunk 문맥과 함께 한 번에 전달합니다.
  - 설정 후보를 다시 추출하지 않고 주체만 판단하며, 정상 응답으로도 해소하지 못한 후보는 `entity_name="미상"`으로 보존합니다.
- `character_fact_comparison_schemas.py`
  - 캐릭터 비교 LLM 응답과 operation별 target/proposed/removal/temporal 불변식을 검증합니다.
- `character_fact_comparator.py`
  - candidate와 현재 snapshot을 DB 식별자 없는 `P*` 참조로 LLM에 전달합니다.
  - canonical slot, STATUS 제거, 시간 범위 규칙을 추가로 검증하고 내부 ref가 사용자-facing reason에 남지 않게 치환합니다.
- `character_fact_comparison_pipeline.py`
  - Spring에서 비교 후보를 하나씩 claim하고 context 조회, 비교, 완료/실패를 순차 조율합니다.
  - 정확한 stale error code일 때만 최신 snapshot context로 최대 3회 다시 비교하고 후보별 실패를 격리합니다.
- `world_setting_extractor.py`, `world_setting_schemas.py`
  - 청크에서 여러 회차에 재사용 가능한 종족·세력·장소·몬스터·능력 체계·규칙/역사·중요 아이템의 원자 속성을 추출합니다.
  - 현재 소유 상태, 날씨, 단발성 사건은 제외하고 confidence를 `0.65`, `0.80`, `0.95` 중 하나로 제한합니다.
- `world_setting_comparator.py`
  - normalized exact 대상이 없을 때 같은 category의 대상명만 `S*` 참조로 LLM에 전달해 최대 3개를 선택합니다.
  - Backend가 반환한 현재 속성 문맥은 UUID/version 없이 `T*` 참조로 LLM에 전달해 ADD/UPDATE/MERGE/EXCLUDE를 판단합니다.
  - 기존 속성과 중복되어 EXCLUDE하면 해당 `T*`와 실제 속성명을 검증해 Backend가 기존값을 저장할 수 있게 전달합니다.
- `world_setting_pipeline.py`
  - 후보 claim, 대상명 페이지 조회, 비교 문맥 조회, 결과 저장을 조율합니다.
  - 문맥 version 충돌은 새 문맥으로 최대 3회 다시 비교하고, 후보 하나의 실패는 해당 후보에만 기록합니다.
- `json_response.py`
  - 세계관 추출·대상 선택·비교가 공유하는 JSON 객체 파싱, Pydantic 검증, 제한 재시도를 담당합니다.
- `schemas.py`
  - LLM에서 받은 설정 후보 JSON을 검증하기 위한 Python 내부 schema를 정의합니다.
  - FastAPI 응답 DTO가 아니라, 외부 LLM 출력이 저장 가능한 구조인지 확인하는 경계 객체입니다.
  - 필수 필드 누락, 잘못된 값 타입, 빈 근거 문장과 후보 종류별 payload 불일치는 이 단계에서 걸러집니다.
- `exceptions.py`
  - Analysis 내부 흐름에서만 사용하는 예외를 정의합니다.
  - FastAPI 응답용 공통 예외와 분리해 Worker가 분석 실패 사유를 구분할 수 있게 합니다.

## 실패 메시지 처리

LLM 응답 파싱/검증 실패 메시지와 JSON 객체 파싱은 `json_response.py`에서 공통 처리합니다. 캐릭터 추출은 현재 chunk ID를 응답에 주입하는 고유 단계가 있어 자체 retry loop를 유지하되 같은 오류 메시지 helper를 사용합니다.

## 재시도 기준

`CharacterSettingExtractor`는 LLM 응답이 JSON으로 파싱되지 않거나, `app/analysis/schemas.py`의 Pydantic schema 검증에 실패한 경우에만 재시도합니다.
설정 후보 배열이 응답 중간에 잘리는 위험을 줄이기 위해 각 추출 요청의 `max_output_tokens`는 4000으로 고정합니다.

캐릭터 Fact 비교와 세계관 추출·대상 선택·비교도 JSON/schema/참조 검증 실패만 설정된 횟수만큼 재시도합니다. 캐릭터 비교는 canonical slot과 STATUS 제거·시간 범위 불변식을, 세계관 대상 선택은 입력 ref의 중복·누락 범위를, 세계관 비교는 operation별 target/property와 제안 문자열을 Python에서 추가 검증합니다. DB 문맥 충돌은 LLM 응답 오류와 별도로 각 pipeline이 최신 문맥을 다시 받아 최대 3회 처리합니다.

예를 들어 다음 경우는 재시도 대상입니다.

- JSON 문법이 깨진 응답
- 필수 필드 누락
- `value_type` enum 범위 밖 값
- `confidence`가 0~1 범위를 벗어난 값
- `SETTING`인데 `attribute_name`, `value_type`, `value_json`이 없거나, `CHARACTER_DISCOVERY`인데 설정 값 필드가 채워진 값

반대로 프롬프트 정책상 좋지 않은 값이더라도 schema상 문자열로 유효하면 현재는 재시도하지 않습니다.

`source_chunk_id`는 이 재시도 정책의 예외입니다. LLM이 생성할 필드가 아니라 호출자가 이미 알고 있는 `EpisodeChunk.id`이므로, 응답에 값이 없거나 잘못된 UUID가 있어도 현재 입력 ID로 덮어쓴 뒤 검증합니다. 따라서 LLM의 UUID 복사 실수로 같은 요청을 반복하지 않습니다.

예를 들어 다음 값은 현재 schema 검증만으로는 통과할 수 있습니다.

- `attribute_name: "item"`
- `attribute_name: "status"`
- `attribute_name: "time. 이름 부여"`
- `attribute_name: "skill.리더십"`
- `confidence: 0.0`

여기서 `time. 이름 부여`는 운영 프롬프트가 지원하는 설정 유형이 아니라, `attribute_name`의 Pydantic shape 검증만으로는 프롬프트 정책 위반 문자열을 차단하지 못한다는 예시입니다. 운영 프롬프트는 시간·사건·타임라인 정보와 제공된 schema에 대응하지 않는 설정을 추출 대상에서 제외합니다.

이런 정책 위반을 Python에서도 강제로 거절하거나 후보 제외 조건으로 만들려면 `ExtractedSettingCandidate`에 attribute 규칙 validator를 추가하거나, schema 검증 이후 별도 policy validation 단계를 둡니다.

## 설정 후보 중복 제거 정책

프롬프트는 같은 청크에서 동일 주체·설정 key·구조화 값을 근거 문장마다 반복 반환하지 않고 가장 명확한 근거 하나만 고르도록 요구합니다. 청크별 LLM 호출은 다른 청크의 결과를 알 수 없으므로, 저장 직전 `SettingCandidateService`가 같은 분석 작업 전체를 한 번 더 중복 제거합니다.

중복 key는 기존 캐릭터와 매칭되었으면 캐릭터 ID, 아니면 정규화한 구체 `entity_name`을 주체로 사용하고, 여기에 `attribute_name`, `value_type`, key 순서를 정규화한 `value_json`을 결합합니다. `attribute_value`는 표시 문구이므로 중복 판정에 사용하지 않습니다. 중복이면 confidence가 더 높은 후보 하나를 남기고, 같으면 먼저 나온 근거를 유지합니다.

동일 `attribute_name`이라도 `value_json`이 다르면 실제 값 변경일 수 있어 모두 유지합니다. `AMBIGUOUS` 주체는 같은 `미상` 문자열이어도 서로 다른 인물일 수 있으므로 중복 제거하지 않습니다.

세계관 후보는 캐릭터 후보와 달리 작가가 확정할 최종 설정 key가 검토 단위입니다. 모든 chunk 추출을 모은 뒤 정규화한 `category + subject_name + setting_name`이 같으면 후보 하나로 통합하고, 서로 다른 추출값은 줄 단위 원본 목록으로, `evidence_spans`와 raw extraction payload는 합집합으로 보존합니다. 2차 비교는 값 하나를 `SINGLE`, 양립 가능한 여러 값을 `MERGED`, 동시에 참일 수 없는 여러 값을 `CONFLICT`로 판정합니다. `MERGED`는 중복을 제거한 자연스러운 최종 문자열을 제안하지만 `CONFLICT`는 추출값 목록을 바꾸지 않고 사용자가 최종값을 정하도록 남깁니다.

## 캐릭터명 매칭 정책

LLM은 기존 캐릭터 DB와의 확정 매칭을 하지 않습니다. LLM은 원문에 실제 나온 표현인 `raw_entity_mention`과 원문 맥락에서 정리한 표시 후보명인 `entity_name`만 반환합니다.

저장 직전 Python resolver가 Spring claim payload의 `knownCharacters`를 받아 다음 순서로 매칭합니다.

`candidate_kind=CHARACTER_DISCOVERY`는 `entity_name` 자체가 발견한 이름이라는 별도 계약을 사용합니다. 따라서 `raw_entity_mention="케닉의 넷째 아들 세룸"`에 기존 캐릭터 `케닉`이 포함돼도 케닉으로 연결하지 않고, `entity_name="세룸"`과 기존 이름 목록만 비교합니다. 기존 이름과 매칭되면 발견 후보를 저장하지 않고, 매칭되지 않으면 `UNRESOLVED` 검토 후보로 저장합니다. 발견 후보는 subject fallback 대상이 아닙니다.

일반 `SETTING` 후보에서도 등록되지 않은 구체 `entity_name`이 `raw_entity_mention` 안에 직접 등장하면, 같은 표현에 함께 나온 기존 관계자 이름보다 새 주체명을 우선해 `UNRESOLVED`로 남깁니다.

```text
raw_entity_mention 정규화
entity_name 정규화
knownCharacters 이름을 한 번 정규화
-> raw match 후보 계산
-> entity match 후보 계산
-> 아래 우선순위로 match_status 결정
```

`raw_entity_mention`은 원문에 실제 등장한 표현이므로 우선권을 갖습니다. `entity_name`은 LLM이 같은 청크 문맥에서 정리한 후보명이므로, raw가 명확하지 않거나 충돌 여부를 확인할 때 보조로 사용합니다.

| 상황 | 결과 | 이유 |
| --- | --- | --- |
| `raw_entity_mention`이 `나`, `내 캐릭터`, `주인공`, `그`, `그녀` 같은 지칭어 + entity가 기존 캐릭터 1명과 매칭 | `MATCHED` | 같은 청크에서 LLM이 구체화한 후보명이 기존 캐릭터 하나와 유일하게 연결되면 문맥 추론을 살림 |
| `raw_entity_mention`이 지칭어 + entity가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | LLM 정리명만으로도 하나를 고를 수 없음 |
| entity가 없거나 `미상`/지칭어 같은 구체적이지 않은 값 | LLM subject fallback 대상 | raw 표현의 형태와 관계없이 previous/current/next chunk 문맥으로 주체만 해소한 뒤 일반 매칭 로직으로 진행 |
| `raw_entity_mention`이 지칭어 + entity가 기존 캐릭터와 매칭 실패 | `UNRESOLVED` | 기존 캐릭터와 연결할 근거는 없지만 신규 캐릭터 후보일 수 있음 |
| subject fallback 정상 응답에서도 주체를 해소하지 못함 | `AMBIGUOUS` | 후보의 설정과 근거는 보존하고 사용자가 캐릭터 연결을 판단하도록 `entity_name="미상"`으로 정규화 |
| raw가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | 어느 캐릭터인지 하나로 확정할 수 없음 |
| raw가 기존 캐릭터 1명과 매칭 + entity가 다른 기존 캐릭터 1명과 매칭 | `AMBIGUOUS` | 원문 표현과 LLM 정리명이 서로 다른 캐릭터를 가리키는 충돌 |
| raw가 기존 캐릭터 1명과 매칭 + entity가 없거나 같은 캐릭터와 매칭 | `MATCHED` | 원문 표현을 우선해 `matched_character_id`를 채움 |
| raw는 매칭 실패 + entity가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | LLM 정리명만으로도 하나를 고를 수 없음 |
| raw는 매칭 실패 + entity가 기존 캐릭터 1명과 매칭 | `MATCHED` | 원문 표현은 설명형이거나 지칭어일 수 있지만 LLM 정리명이 한 명과만 연결됨 |
| raw와 entity 모두 기존 캐릭터와 매칭 실패 | `UNRESOLVED` | 기존 캐릭터와 연결할 근거가 없음. 신규 캐릭터 후보일 수 있음 |

매칭 방식은 완전 일치를 먼저 보고, 이후 한쪽 이름이 다른 쪽에 포함되는 경우를 확인합니다. 단, 한 글자 이름/표현은 오탐이 많으므로 포함 관계 매칭에서 제외합니다.

### adjacent chunk subject fallback

`entity_name`이 비어 있거나 `미상`, `불명`, `나`, `그녀`, `주인공`처럼 구체적인 캐릭터명이 아닌 후보는 current chunk만으로 주체가 풀리지 않은 상태입니다. `raw_entity_mention`은 fallback 판단에 사용할 입력이지만, 그 값이 미리 정한 지칭어 목록에 들어가는지를 fallback 진입 조건으로 사용하지 않습니다.

이 경우 단순히 주변 청크에서 기존 캐릭터 이름을 문자열로 찾지 않습니다. 주변에 이름이 등장한다는 사실만으로 지칭 대상을 확정하면 잘못된 캐릭터 설정이 저장될 수 있기 때문입니다.

현재 구현은 fallback 대상 후보를 current chunk 기준으로 묶고, previous/current/next chunk 문맥과 함께 LLM subject resolver에 전달합니다.

fallback 진입/처리 기준:

| 상황 | fallback 호출 | 처리 |
| --- | --- | --- |
| raw가 지칭어이고 entity가 기존 캐릭터 1명과 매칭 | 호출하지 않음 | 기존 매칭 로직에서 `MATCHED` |
| raw가 지칭어이고 entity가 기존 캐릭터 여러 명과 매칭 | 호출하지 않음 | 기존 매칭 로직에서 `AMBIGUOUS` |
| raw가 지칭어이고 entity가 기존 캐릭터와 매칭 실패 | 호출하지 않음 | 신규 캐릭터 가능성이 있으므로 `UNRESOLVED` |
| entity가 없거나 `미상`/지칭어 같은 구체적이지 않은 값 | 호출함 | raw가 없거나 예상하지 못한 원문 표현이어도 previous/current/next chunk로 주체를 재판단 |
| fallback 응답의 `resolved_entity_name`이 구체 이름 | - | candidate의 `entity_name`만 치환하고 기존 매칭 로직으로 진행 |
| fallback 응답의 `resolved_entity_name`이 null | - | 원래 후보를 보존하고 `entity_name="미상"`으로 정규화한 뒤 기존 매칭 로직에서 `AMBIGUOUS` 처리 |
| fallback 응답의 `resolved_entity_name`이 `미상`, `그녀`, `주인공` 같은 placeholder/지칭어 | - | null과 같은 정상적인 해소 실패로 보고 후보를 `미상`으로 보존 |
| 응답 JSON/schema가 잘못되거나 candidate ID가 누락·중복·추가됨 | - | 사용자 판단 대상이 아닌 기술적 계약 오류이므로 분석 실패로 전파 |

```text
raw_entity_mention이 지칭어 + entity_name이 기존 캐릭터 1명과 매칭
-> MATCHED

raw_entity_mention이 지칭어 + entity_name이 기존 캐릭터 여러 명과 매칭
-> AMBIGUOUS

entity_name이 "미상" 또는 지칭어 같은 구체적이지 않은 값
-> 같은 current chunk의 fallback 대상 후보를 batch로 묶음
-> previous/current/next chunk와 knownCharacters를 LLM subject resolver에 전달
-> resolved_entity_name이 구체 캐릭터명이면 entity_name만 치환한 뒤 일반 매칭 로직으로 진행
-> resolved_entity_name이 null, "미상", "그녀" 같은 placeholder/지칭어이면 entity_name을 "미상"으로 정규화
-> character_name_resolver가 AMBIGUOUS로 계산해 사용자 검토 후보로 저장

raw_entity_mention이 지칭어 + entity_name이 기존 캐릭터와 매칭 실패
-> UNRESOLVED

raw_entity_mention이 지칭어 + entity_name이 이미 구체 후보명
-> fallback을 호출하지 않고 entity_name 기준 매칭 정책으로 진행
```

fallback은 설정 후보 추출을 다시 하는 단계가 아니라, 이미 추출된 후보의 주체만 해소하는 좁은 resolver입니다. previous/next chunk는 판단 문맥으로만 사용하고, `source_chunk_id`, `evidence_spans`, offset 기준은 후보가 실제 추출된 current chunk를 유지합니다.

LLM subject resolver는 `MATCHED`, `UNRESOLVED`, `AMBIGUOUS` 같은 최종 매칭 상태를 판단하지 않습니다. LLM이 확실한 주체명만 `resolved_entity_name`으로 반환하면 Python이 후보의 `entity_name`만 치환하고, 이후 기존 `character_name_resolver`가 `knownCharacters`와 비교해 최종 `matched_character_id`, `match_status`를 계산합니다.

`resolved_entity_name`에는 `미상`, `불명`, `unknown`, `나`, `그`, `그녀`, `주인공` 같은 placeholder/지칭어가 들어오면 안 됩니다. LLM이 정상 응답에서 null 또는 이런 값을 반환하면 Python은 실제 해소 실패로 보고 원래 후보를 `entity_name="미상"`으로 보존합니다. 이후 기존 `character_name_resolver`가 이를 `AMBIGUOUS`로 계산하므로 `UNRESOLVED`의 새 캐릭터 후보로 잘못 표시되지 않습니다.

응답 파싱/schema 검증 실패와 candidate ID 누락·중복·추가는 의미상 해소 실패가 아니라 외부 응답 계약 위반입니다. 이런 기술적 실패는 `AMBIGUOUS`로 숨기지 않고 분석 실패로 전파합니다.

예시 입력:

```json
{
  "known_characters": [
    {
      "character_id": "00000000-0000-0000-0000-000000000101",
      "name": "비요른 얀델"
    }
  ],
  "context": {
    "previous_chunk": "비요른 얀델은 낡은 도끼를 들고 있었다.",
    "current_chunk": "나는 1레벨 바바리안으로 깨어났다.",
    "next_chunk": "주변에는 다른 인물이 없었다."
  },
  "candidates": [
    {
      "candidate_id": "candidate-0",
      "raw_entity_mention": "나는",
      "entity_name": "미상",
      "attribute_name": "level",
      "attribute_value": "1",
      "evidence_quotes": ["나는 1레벨 바바리안으로 깨어났다."]
    }
  ]
}
```

예시 응답:

```json
{
  "resolutions": [
    {
      "candidate_id": "candidate-0",
      "resolved_entity_name": "비요른 얀델",
      "reason": "앞뒤 문맥에서 1인칭 서술 주체가 비요른 얀델로 이어진다."
    }
  ]
}
```

처리 결과:

```text
candidate-0.entity_name = "비요른 얀델"로 치환
attribute/value/evidence/source_chunk는 유지
character_name_resolver가 기존 캐릭터 목록과 비교해 MATCHED / UNRESOLVED / AMBIGUOUS 계산
```

해소할 수 없는 경우:

```json
{
  "resolutions": [
    {
      "candidate_id": "candidate-0",
      "resolved_entity_name": null,
      "reason": "앞뒤 문맥만으로 주체를 특정할 수 없다."
    }
  ]
}
```

이 경우 원래 후보의 설정값, 근거, source chunk를 유지하고 `entity_name`만 `"미상"`으로 정규화합니다. LLM이 `resolved_entity_name`에 `"미상"` 또는 `"그녀"` 같은 문자열을 넣어도 같은 방식으로 보존하며, 저장 단계의 기존 캐릭터명 매칭 로직이 최종 상태를 `AMBIGUOUS`로 계산합니다.

### subject fallback trace 정책

현재 저장/출력 구조에서는 fallback 전체 개수만 summary로 확인할 수 있습니다.

```text
subjectFallbackCallCount
subjectFallbackResolvedCount
subjectFallbackUnresolvedCount
```

`subjectFallbackUnresolvedCount`는 subject resolver가 정상 응답을 반환했지만 구체 이름을 찾지 못해 `미상`으로 보존한 후보 수입니다. 최종 `AMBIGUOUS` 상태는 이후 기존 캐릭터명 매칭 단계에서 계산하므로, subject resolver 내부 지표에는 `Ambiguous` 대신 `Unresolved`를 사용합니다.

최종 `settingCandidates[]`에서는 해소 실패 후보가 `미상 + AMBIGUOUS`로 보존된 사실을 볼 수 있습니다. 다만 어떤 chunk에서 fallback이 호출됐는지, LLM이 null을 반환한 이유가 무엇인지, 원래 `entity_name`이 무엇이었는지는 별도 trace 없이는 알 수 없습니다.

후보별 fallback 이력을 확인하려면 별도 trace 구조가 필요합니다.

예시:

```json
{
  "chunk_index": 7,
  "source_chunk_id": "chunk-id",
  "candidate_id": "candidate-0",
  "raw_entity_mention": "나는",
  "original_entity_name": "미상",
  "resolved_entity_name": "비요른 얀델",
  "result": "RESOLVED",
  "unresolved_reason": null
}
```

다만 이 trace를 어디까지 남길지는 정책 결정이 필요합니다.

| 선택지 | 장점 | 주의점 |
| --- | --- | --- |
| debug runner JSON에만 남김 | 로컬 검증과 PR 리뷰에 충분하고 DB 영향이 없음 | 운영 이력으로는 조회할 수 없음 |
| Worker summary JSON에 요약/샘플만 남김 | 분석 job 단위 관측성이 생김 | summary가 커질 수 있어 개수 제한 정책 필요 |
| `setting_candidates.raw_ai_result_json`에 후보별 trace를 남김 | 저장된 후보와 fallback 이력을 함께 볼 수 있음 | 현재 값에는 fallback 응답과 판단 사유가 포함되지 않으므로 별도 구조가 필요 |
| 별도 로그/실패 이력 테이블에 남김 | 운영 디버깅에 가장 강함 | 스키마와 보존 기간 정책이 필요 |

현재 구현은 trace를 저장하지 않고 count만 남깁니다. 후보별 fallback 위치와 해소 실패 사유를 제품/운영에서 조회해야 한다면, 위 선택지 중 하나를 정한 뒤 debug 출력, Worker summary, DB 저장 범위를 함께 조정합니다.

`subjectFallbackUnresolvedCount`에는 LLM fallback 정상 응답으로도 구체 이름을 찾지 못해 `미상`으로 보존된 후보만 포함됩니다. malformed 응답이나 candidate ID 계약 위반은 분석 실패이므로 이 개수에 포함하지 않습니다.

## 후속 작업

- NVM-143에서 설정 후보와 기존 fact, 직접 근거, pgvector Top-K 결과를 조합합니다.
- NVM-144에서 NVM-143이 모은 검증 문맥을 기준으로 최종 충돌 여부를 판정합니다.
- 설정 추출 재시도와 subject fallback을 포함한 LLM token usage를 Worker 단위로 집계해 Spring 완료 보고에 연결합니다.
- 프롬프트 정책 위반 후보를 schema validator, 후처리 필터, LLM 재시도 중 어디에서 다룰지 결정합니다.
- subject fallback의 prompt 품질과 호출 단위가 충분한지 실제 원문으로 검증합니다.
- fallback에서 해소된 후보와 `미상`으로 보존된 후보의 trace를 debug JSON, Worker summary, DB 중 어디에 남길지 정책을 결정합니다.
