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

저장 직전 Python resolver가 known characters context를 받아 다음 기준으로 매칭합니다.

- 기존 캐릭터 이름 또는 별칭과 정확히 1개만 연결되면 `MATCHED`로 저장하고 `matched_character_id`를 채웁니다.
- 기존 후보가 없으면 `UNRESOLVED`로 저장합니다.
- `나`, `내 캐릭터`, `주인공`, `그`, `그녀` 같은 지칭어이거나 여러 기존 캐릭터에 걸리면 `AMBIGUOUS`로 저장합니다.

현재 worker 기본 흐름은 Spring claim payload의 `knownCharacters`를 resolver에 전달합니다. 별도 테스트나 대체 실행 경로에서는 `SettingCandidateService`의 provider 훅으로 known characters를 주입할 수 있습니다.

## 후속 작업

- 기존 확정 설정과 비교하는 충돌 검사 흐름을 연결합니다.
- 프롬프트 정책 위반 후보를 schema validator, 후처리 필터, LLM 재시도 중 어디에서 다룰지 결정합니다.
- `AMBIGUOUS` 중 화자/대명사 후보에 한해 adjacent chunk를 참고하는 resolver fallback을 검토합니다.
