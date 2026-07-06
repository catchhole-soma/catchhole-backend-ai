# analysis

AI 분석 유스케이스와 분석 판단 로직을 두는 패키지입니다.

Spring 기준으로는 여러 하위 기능을 조합해 도메인 분석 결과를 만드는 Service/Domain Service에 가깝습니다.

## 역할

- 원문 청크를 입력으로 받아 설정 후보를 추출합니다.
- LLM 응답 JSON을 Python 내부 검증 schema로 확인합니다.
- 추출 결과를 `setting_candidates` 저장 구조에 맞는 중간 결과로 정리합니다.
- 추출 후보의 캐릭터명 표현을 기존 캐릭터 목록과 비교해 매칭 상태를 계산합니다.
- 후속 단계에서 근거 위치 계산, 충돌 검사, 요약 생성 로직을 연결합니다.

다음 책임은 Analysis에 넣지 않습니다.

- OpenAI HTTP 호출 세부 구현
- S3 원문 조회
- SQLAlchemy query 세부 작성
- Spring 내부 Worker API 호출

## 현재 파일

- `setting_extractor.py`
  - 청크 하나를 LLM에 보내 캐릭터 설정 후보를 추출합니다.
  - prompt 로드, user prompt 구성, JSON 파싱, schema 검증, 검증 실패 재시도를 담당합니다.
- `evidence_span_resolver.py`
  - LLM이 반환한 `evidence_spans[].quote`를 청크 원문에서 다시 찾아 offset을 보정합니다.
  - exact match를 우선 사용하고, 실패하면 공백/줄바꿈 정규화 기반 검색을 시도합니다.
  - quote를 찾지 못하면 잘못된 위치를 저장하지 않도록 offset을 null로 유지합니다.
- `character_name_resolver.py`
  - `KnownCharacter` 목록과 추출 후보의 `raw_entity_mention`, `entity_name`을 비교합니다.
  - 기존 캐릭터 하나와 확실히 연결되면 `MATCHED`, 후보가 없으면 `UNRESOLVED`, 대명사/복수 후보처럼 위험하면 `AMBIGUOUS`를 반환합니다.
- `character_subject_resolver.py`
  - `raw_entity_mention`이 지칭어이고 `entity_name`이 `미상`/지칭어 같은 placeholder인 후보를 LLM으로 한 번 더 해소합니다.
  - 같은 current chunk에서 나온 fallback 대상 후보를 묶어 previous/current/next chunk 문맥과 함께 한 번에 전달합니다.
  - 설정 후보를 다시 추출하지 않고 주체만 판단하며, 실패한 placeholder 후보는 저장 전 폐기합니다.
- `schemas.py`
  - LLM에서 받은 설정 후보 JSON을 검증하기 위한 Python 내부 schema를 정의합니다.
  - FastAPI 응답 DTO가 아니라, 외부 LLM 출력이 저장 가능한 구조인지 확인하는 경계 객체입니다.
  - 필수 필드 누락, 잘못된 값 타입, 빈 근거 문장 등은 이 단계에서 걸러집니다.
- `exceptions.py`
  - Analysis 내부 흐름에서만 사용하는 예외를 정의합니다.
  - FastAPI 응답용 공통 예외와 분리해 Worker가 분석 실패 사유를 구분할 수 있게 합니다.

## 실패 메시지 처리

현재 LLM 응답 파싱/검증 실패 메시지는 `setting_extractor.py` 내부 helper에서 짧게 정리합니다.

아직 사용처가 `CharacterSettingExtractor` 하나뿐이므로 공통 util로 분리하지 않았습니다.
다만 이후 Worker 실패 보고, Spring 내부 API 실패 보고, S3/DB 처리 실패 등에서 같은 규칙이 필요해지면 `app/core/error_messages.py` 같은 공통 helper로 분리합니다.

## 재시도 기준

`CharacterSettingExtractor`는 LLM 응답이 JSON으로 파싱되지 않거나, `app/analysis/schemas.py`의 Pydantic schema 검증에 실패한 경우에만 재시도합니다.

예를 들어 다음 경우는 재시도 대상입니다.

- JSON 문법이 깨진 응답
- 필수 필드 누락
- UUID 형식 오류
- `value_type` enum 범위 밖 값
- `confidence`가 0~1 범위를 벗어난 값

반대로 프롬프트 정책상 좋지 않은 값이더라도 schema상 문자열로 유효하면 현재는 재시도하지 않습니다.

예를 들어 다음 값은 현재 schema 검증만으로는 통과할 수 있습니다.

- `attribute_name: "items"`
- `attribute_name: "status"`
- `attribute_name: "time. 이름 부여"`
- `attribute_name: "skills.리더십"`
- `confidence: 0.0`

이런 정책 위반을 재시도 또는 후보 제외 조건으로 만들려면 `ExtractedSettingCandidate`에 attribute 규칙 validator를 추가하거나, schema 검증 이후 별도 policy validation 단계를 둡니다.

## 캐릭터명 매칭 정책

LLM은 기존 캐릭터 DB와의 확정 매칭을 하지 않습니다. LLM은 원문에 실제 나온 표현인 `raw_entity_mention`과 원문 맥락에서 정리한 표시 후보명인 `entity_name`만 반환합니다.

저장 직전 Python resolver가 Spring claim payload의 `knownCharacters`를 받아 다음 순서로 매칭합니다.

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
| `raw_entity_mention`이 지칭어 + entity가 없거나 `미상`/지칭어 같은 placeholder | LLM subject fallback 대상 | previous/current/next chunk 문맥으로 주체만 해소한 뒤 일반 매칭 로직으로 진행 |
| `raw_entity_mention`이 지칭어 + entity가 기존 캐릭터와 매칭 실패 | `UNRESOLVED` | 기존 캐릭터와 연결할 근거는 없지만 신규 캐릭터 후보일 수 있음 |
| `raw_entity_mention` 없음 + `entity_name`도 없거나 `미상`/지칭어 같은 placeholder | 저장 전 제외 | 원문 표현과 구체 후보명이 모두 없어 검토자가 해소할 근거가 없음 |
| raw가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | 어느 캐릭터인지 하나로 확정할 수 없음 |
| raw가 기존 캐릭터 1명과 매칭 + entity가 다른 기존 캐릭터 1명과 매칭 | `AMBIGUOUS` | 원문 표현과 LLM 정리명이 서로 다른 캐릭터를 가리키는 충돌 |
| raw가 기존 캐릭터 1명과 매칭 + entity가 없거나 같은 캐릭터와 매칭 | `MATCHED` | 원문 표현을 우선해 `matched_character_id`를 채움 |
| raw는 매칭 실패 + entity가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | LLM 정리명만으로도 하나를 고를 수 없음 |
| raw는 매칭 실패 + entity가 기존 캐릭터 1명과 매칭 | `MATCHED` | 원문 표현은 설명형이거나 지칭어일 수 있지만 LLM 정리명이 한 명과만 연결됨 |
| raw와 entity 모두 기존 캐릭터와 매칭 실패 | `UNRESOLVED` | 기존 캐릭터와 연결할 근거가 없음. 신규 캐릭터 후보일 수 있음 |

매칭 방식은 완전 일치를 먼저 보고, 이후 한쪽 이름이 다른 쪽에 포함되는 경우를 확인합니다. 단, 한 글자 이름/표현은 오탐이 많으므로 포함 관계 매칭에서 제외합니다.

### adjacent chunk subject fallback

`raw_entity_mention`이 `나`, `나는`, `그`, `그녀는`, `주인공` 같은 지칭어이고 `entity_name`이 `미상`/지칭어 같은 placeholder인 후보는 current chunk만으로 주체가 풀리지 않은 상태입니다.

이 경우 단순히 주변 청크에서 기존 캐릭터 이름을 문자열로 찾지 않습니다. 주변에 이름이 등장한다는 사실만으로 지칭 대상을 확정하면 잘못된 캐릭터 설정이 저장될 수 있기 때문입니다.

현재 구현은 fallback 대상 후보를 current chunk 기준으로 묶고, previous/current/next chunk 문맥과 함께 LLM subject resolver에 전달합니다.

fallback 진입/처리 기준:

| 상황 | fallback 호출 | 처리 |
| --- | --- | --- |
| raw가 지칭어이고 entity가 기존 캐릭터 1명과 매칭 | 호출하지 않음 | 기존 매칭 로직에서 `MATCHED` |
| raw가 지칭어이고 entity가 기존 캐릭터 여러 명과 매칭 | 호출하지 않음 | 기존 매칭 로직에서 `AMBIGUOUS` |
| raw가 지칭어이고 entity가 기존 캐릭터와 매칭 실패 | 호출하지 않음 | 신규 캐릭터 가능성이 있으므로 `UNRESOLVED` |
| raw가 지칭어이고 entity가 없거나 `미상`/지칭어 같은 placeholder | 호출함 | previous/current/next chunk로 주체만 재판단 |
| raw가 없고 entity도 없거나 `미상`/지칭어 같은 placeholder | 호출하지 않음 | fallback에 보낼 원문 지칭 표현이 없으므로 저장 전 제외 |
| fallback 응답의 `resolved_entity_name`이 구체 이름 | - | candidate의 `entity_name`만 치환하고 기존 매칭 로직으로 진행 |
| fallback 응답의 `resolved_entity_name`이 null | - | 잘못된 placeholder 후보가 저장되지 않도록 폐기 |
| fallback 응답의 `resolved_entity_name`이 `미상`, `그녀`, `주인공` 같은 placeholder/지칭어 | - | 실제 해소 실패로 보고 폐기 |

```text
raw_entity_mention이 지칭어 + entity_name이 기존 캐릭터 1명과 매칭
-> MATCHED

raw_entity_mention이 지칭어 + entity_name이 기존 캐릭터 여러 명과 매칭
-> AMBIGUOUS

raw_entity_mention이 지칭어 + entity_name이 "미상" 또는 지칭어 같은 placeholder
-> 같은 current chunk의 fallback 대상 후보를 batch로 묶음
-> previous/current/next chunk와 knownCharacters를 LLM subject resolver에 전달
-> resolved_entity_name이 구체 캐릭터명이면 entity_name만 치환한 뒤 일반 매칭 로직으로 진행
-> resolved_entity_name이 null, "미상", "그녀" 같은 placeholder/지칭어이면 저장 전 폐기

raw_entity_mention이 지칭어 + entity_name이 기존 캐릭터와 매칭 실패
-> UNRESOLVED

raw_entity_mention이 지칭어 + entity_name이 이미 구체 후보명
-> fallback을 호출하지 않고 entity_name 기준 매칭 정책으로 진행

raw_entity_mention이 없고 entity_name도 "미상" 또는 지칭어 같은 placeholder
-> fallback에 사용할 원문 표현이 없으므로 저장 전 제외
```

fallback은 설정 후보 추출을 다시 하는 단계가 아니라, 이미 추출된 후보의 주체만 해소하는 좁은 resolver입니다. previous/next chunk는 판단 문맥으로만 사용하고, `source_chunk_id`, `evidence_spans`, offset 기준은 후보가 실제 추출된 current chunk를 유지합니다.

LLM subject resolver는 `MATCHED`, `UNRESOLVED`, `AMBIGUOUS` 같은 최종 매칭 상태를 판단하지 않습니다. LLM이 확실한 주체명만 `resolved_entity_name`으로 반환하면 Python이 후보의 `entity_name`만 치환하고, 이후 기존 `character_name_resolver`가 `knownCharacters`와 비교해 최종 `matched_character_id`, `match_status`를 계산합니다.

`resolved_entity_name`에는 `미상`, `불명`, `unknown`, `나`, `그`, `그녀`, `주인공` 같은 placeholder/지칭어가 들어오면 안 됩니다. LLM이 이런 값을 반환하더라도 Python은 실제 해소 실패로 보고 해당 fallback 후보를 저장하지 않습니다.

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

이 경우 placeholder 후보는 `setting_candidates` 저장 전에 제외합니다. LLM이 `resolved_entity_name`에 `"미상"` 또는 `"그녀"` 같은 문자열을 넣어도 같은 방식으로 제외합니다.

### subject fallback trace 정책

현재 저장/출력 구조에서는 fallback 전체 개수만 summary로 확인할 수 있습니다.

```text
subjectFallbackCallCount
subjectFallbackResolvedCount
subjectFallbackDiscardedCount
```

따라서 어떤 후보가 fallback 대상이었는지, 어떤 chunk에서 fallback이 호출됐는지, 폐기된 후보가 무엇이었는지는 최종 `settingCandidates[]`만으로는 알 수 없습니다. 최종 후보에는 fallback 성공 후의 `entity_name`만 남고, fallback 실패 후보는 저장 전에 제외되기 때문입니다.

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
  "discard_reason": null
}
```

다만 이 trace를 어디까지 남길지는 정책 결정이 필요합니다.

| 선택지 | 장점 | 주의점 |
| --- | --- | --- |
| debug runner JSON에만 남김 | 로컬 검증과 PR 리뷰에 충분하고 DB 영향이 없음 | 운영 이력으로는 조회할 수 없음 |
| Worker summary JSON에 요약/샘플만 남김 | 분석 job 단위 관측성이 생김 | summary가 커질 수 있어 개수 제한 정책 필요 |
| `setting_candidates.raw_ai_result_json`에 후보별 trace를 남김 | 저장된 후보와 fallback 이력을 함께 볼 수 있음 | 실패 후 폐기된 후보는 저장 후보가 없어 남기기 어려움 |
| 별도 로그/실패 이력 테이블에 남김 | 운영 디버깅에 가장 강함 | 스키마와 보존 기간 정책이 필요 |

현재 구현은 trace를 저장하지 않고 count만 남깁니다. 후보별 fallback 위치와 폐기 사유를 제품/운영에서 조회해야 한다면, 위 선택지 중 하나를 정한 뒤 debug 출력, Worker summary, DB 저장 범위를 함께 조정합니다.

`subjectFallbackDiscardedCount`에는 LLM fallback 응답으로 해소되지 않아 폐기된 후보와, raw/entity 모두 주체 판단에 쓸 수 없어 LLM 호출 전 제외된 후보가 함께 포함됩니다.

## 후속 작업

- 기존 확정 설정과 비교하는 충돌 검사 흐름을 연결합니다.
- 프롬프트 정책 위반 후보를 schema validator, 후처리 필터, LLM 재시도 중 어디에서 다룰지 결정합니다.
- subject fallback의 prompt 품질과 호출 단위가 충분한지 실제 원문으로 검증합니다.
- fallback에서 폐기된 후보와 해소된 후보의 trace를 debug JSON, Worker summary, DB 중 어디에 남길지 정책을 결정합니다.
