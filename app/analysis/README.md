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
| `raw_entity_mention`이 `나`, `내 캐릭터`, `주인공`, `그`, `그녀` 같은 지칭어 | `AMBIGUOUS` | 같은 청크에서 LLM이 `entity_name`을 추론했더라도 화자/지칭 대상이 항상 안전하게 확정되지는 않기 때문 |
| `raw_entity_mention` 없음 + `entity_name`도 지칭어 | `AMBIGUOUS` | 비교 가능한 명확한 캐릭터명이 없음 |
| raw가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | 어느 캐릭터인지 하나로 확정할 수 없음 |
| raw가 기존 캐릭터 1명과 매칭 + entity가 다른 기존 캐릭터 1명과 매칭 | `AMBIGUOUS` | 원문 표현과 LLM 정리명이 서로 다른 캐릭터를 가리키는 충돌 |
| raw가 기존 캐릭터 1명과 매칭 + entity가 없거나 같은 캐릭터와 매칭 | `MATCHED` | 원문 표현을 우선해 `matched_character_id`를 채움 |
| raw는 매칭 실패 + entity가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | LLM 정리명만으로도 하나를 고를 수 없음 |
| raw는 매칭 실패 + raw가 지칭어가 아님 + entity가 기존 캐릭터 1명과 매칭 | `MATCHED` | 원문 표현은 설명형이지만 LLM 정리명이 한 명과만 연결됨 |
| raw와 entity 모두 기존 캐릭터와 매칭 실패 | `UNRESOLVED` | 기존 캐릭터와 연결할 근거가 없음. 신규 캐릭터 후보일 수 있음 |

매칭 방식은 완전 일치를 먼저 보고, 이후 한쪽 이름이 다른 쪽에 포함되는 경우를 확인합니다. 단, 한 글자 이름/표현은 오탐이 많으므로 포함 관계 매칭에서 제외합니다.

### adjacent chunk fallback 적용 시 변경 지점

현재 PR에서는 현재 청크 안에서만 캐릭터명을 판단합니다. 따라서 `raw_entity_mention`이 `나`, `그`, `그녀`, `주인공` 같은 지칭어이면 `entity_name`이 있더라도 `character_name_resolver.py`에서 즉시 `AMBIGUOUS`로 반환합니다.

후속 작업에서 previous/current/next chunk를 덧대는 fallback을 적용한다면, 이 early return 지점이 바뀌어야 합니다.

적용 방향은 다음과 같이 봅니다.

```text
현재:
raw_entity_mention이 지칭어
-> 즉시 AMBIGUOUS

후속 fallback 적용 후:
raw_entity_mention이 지칭어 + entity_name이 "미상" 같은 placeholder
-> previous/current/next chunk로 지칭 대상 해소 시도
-> 해소 성공: entity_name을 실제 후보명으로 치환한 뒤 일반 매칭 로직으로 진행
-> 해소 실패: setting_candidates 저장 전 폐기

raw_entity_mention이 지칭어 + entity_name이 이미 구체 후보명
-> fallback을 다시 호출하지 않고 entity_name 기준 매칭 정책으로 넘길지 검토
```

이 fallback은 설정 후보 추출을 다시 하는 단계가 아니라, 이미 추출된 후보의 주체만 해소하는 좁은 resolver로 두는 것이 안전합니다. previous/next chunk는 판단 문맥으로만 사용하고, `source_chunk_id`, `evidence_spans`, offset 기준은 후보가 실제 추출된 current chunk를 유지합니다.

이 정책을 구현하려면 `raw_entity_mention`이 지칭어일 때 무조건 `AMBIGUOUS`로 끝내는 현재 분기를 분리해야 합니다. 특히 fallback으로 해소된 후보를 `MATCHED` 또는 `UNRESOLVED`로 넘길 수 있도록, `character_name_resolver.py`에 "지칭어지만 이미 context-resolved 된 후보"를 구분할 입력 또는 중간 단계가 필요합니다.

## 후속 작업

- 기존 확정 설정과 비교하는 충돌 검사 흐름을 연결합니다.
- 프롬프트 정책 위반 후보를 schema validator, 후처리 필터, LLM 재시도 중 어디에서 다룰지 결정합니다.
- `나`, `그`, `그녀`, `주인공` 같은 지칭어 후보는 현재 PR에서 보수적으로 `AMBIGUOUS`로 남깁니다.
- 후속 작업에서는 중요한 설정 후보지만 현재 청크만으로 주체를 해소하지 못한 경우 `entity_name="미상"`으로 받고, previous/current/next chunk 문맥을 추가로 참고하는 fallback resolver를 검토합니다.
- fallback으로도 캐릭터명을 해소하지 못한 `미상` 후보는 `setting_candidates`에 저장하지 않고 폐기하며, 폐기 개수를 worker summary에 남기는 방향을 검토합니다.
