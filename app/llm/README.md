# llm

LLM provider 호출과 prompt 관리를 담당하는 패키지입니다.

Spring 기준으로는 외부 AI provider adapter에 가깝습니다.

## 역할

- OpenAI 같은 외부 LLM API 호출을 감쌉니다.
- API key, 모델명, endpoint 같은 설정값을 `Settings`에서 주입받습니다.
- prompt 파일을 별도 리소스로 관리합니다.
- LLM 응답 텍스트와 token usage를 Python 객체로 변환합니다.

다음 책임은 LLM 패키지에 넣지 않습니다.

- DB 저장
- Spring 내부 Worker API 보고
- setting_candidates 저장 정책
- 후보 승인/반려 정책

## 현재 파일

- `openai_client.py`
  - OpenAI Responses API를 호출합니다.
  - 원고와 분석 결과가 provider 측에 저장되지 않도록 모든 요청에 `store: false`를 강제합니다.
  - `LLM_API_KEY`, 단계별 `LLM_EXTRACTION_MODEL`·`LLM_SUBJECT_RESOLUTION_MODEL`·`LLM_COMPARISON_MODEL`, fallback `LLM_MODEL`, `LLM_REASONING_EFFORT`, `OPENAI_RESPONSES_API_URL` 설정을 사용합니다.
  - 운영 기본 라우팅은 후보 추출 `gpt-5.6-terra`, 주체 해소·비교 `gpt-5.6-luna`이며 공통 MVP 추론 강도는 `none`입니다.
  - 같은 정적 prompt prefix를 공유하는 호출에는 안정적인 `prompt_cache_key`를 전달합니다.
  - GPT-5.6 explicit cache breakpoint는 아직 사용하지 않으며, 현재는 정적 prefix 우선 배치와 cache key로 implicit cache 재사용을 돕습니다.
  - debug 로그에는 prompt 본문 없이 cached input 필드의 존재 여부와 token usage만 남깁니다.
  - 응답 텍스트와 token usage를 `LlmTextResponse`로 반환합니다.
  - 호출별 `LlmResponseSchema`가 있을 때만 Responses API의 `text.format=json_schema`를 전달합니다. 현재는 캐릭터 설정 추출에만 적용합니다.
- `responses.py`
  - LLM 호출 결과를 내부에서 전달하기 위한 `dataclass` 값 객체를 둡니다.
- `protocols.py`
  - 캐릭터·세계관 분석기와 token metering wrapper가 공유하는 최소 텍스트 생성 계약을 둡니다.
- `prompts/character_setting_extraction.md`
  - 캐릭터 중심 설정 후보 추출 prompt입니다.
- `prompts/character_subject_resolution.md`
  - 구체적이지 않은 `entity_name` 후보의 주체만 해소하는 fallback prompt입니다.
- `prompts/world_setting_extraction.md`
  - 지속 가능한 세계관 속성을 원자 후보로 추출하는 prompt입니다.
- `prompts/world_setting_subject_resolution.md`
  - 같은 category의 기존 대상명 중 의미상 같은 대상 후보를 고르는 prompt입니다.
- `prompts/world_setting_comparison.md`
  - 후보와 기존 속성을 비교해 ADD/UPDATE/MERGE/EXCLUDE를 만드는 prompt입니다.

## 토큰 사용량 상태

`OpenAIResponsesClient`는 OpenAI 응답의 `usage.input_tokens`,
`usage.input_tokens_details.cached_tokens`, `usage.output_tokens`를
`LlmTextResponse`에 담습니다. 캐릭터·세계관 추출, subject fallback과 세계관 비교에 주입된
`MeteredTextGenerationClient`가 provider 호출마다 Spring 원장에 먼저 예약하고,
응답 usage로 실제 사용량을 정산합니다.

```text
CharacterSettingExtractor / CharacterSubjectResolver / WorldSettingExtractor
/ WorldSettingSubjectResolver / WorldSettingComparator
-> MeteredTextGenerationClient.reserve
-> OpenAIResponsesClient
-> LlmTextResponse(input/cached input/output)
-> MeteredTextGenerationClient.settle 또는 release
-> Spring ai_token_usages
```

캐릭터·세계관 추출 재시도, subject fallback, 세계관 대상 탐색·비교도 각각 실제 provider 호출 단위로 기록합니다.
1차 캐릭터 Fact·세계관 후보 추출은 extraction 모델, 캐릭터·세계관 대상 탐색은 subject-resolution 모델, 세계관 비교·재비교는 comparison 모델을 사용합니다. 모델이 달라도 provider 요청과 Spring 토큰 원장에는 각 단계에서 실제 호출한 모델명이 기록됩니다.
`analysis_jobs`의 과거 합산 컬럼을 비용 원장으로 사용하지 않으며, 요청별 근거는
Spring의 `ai_token_usages`를 기준으로 조회합니다.

실제 Prompt Cache와 예약량 검증 결과는
[`docs/ai-token-cache-validation.md`](../../docs/ai-token-cache-validation.md)에 기록합니다.

## 현재 추출 방식

캐릭터 설정 추출은 Pydantic에서 생성한 strict JSON Schema를 Responses API에 전달하고, 응답을 Provider wire model과 저장 경계 model로 두 번 검증합니다. 그 밖의 LLM 호출은 기존 prompt + Python schema 검증을 유지합니다.

JSON 파싱 실패 또는 Python schema 검증 실패는 `CharacterSettingExtractor`에서 재시도합니다. 다음 요청에는 최초 prompt와 값이 제거된 `reasonCode + fieldLocs`만 넣고 실패 응답 원문은 prompt나 로그에 남기지 않습니다. 다만 attribute 이름 정책처럼 schema로 표현하지 않은 프롬프트 정책 위반까지 강제하지는 않습니다.
캐릭터 설정 추출은 `max_output_tokens=6000`에서 시작해 출력 절단 시 12000으로, 세계관 추출은 5000에서 시작해 절단 시 10000으로 한 번만 확장합니다. 주체 해소는 2000, 비교는 3000을 사용하며 절단 확장 재시도는 하지 않습니다.

예를 들어 `attribute_name`이 `item`처럼 suffix 없이 오거나, `confidence`가 `0.0`인 응답은 프롬프트상 원하지 않는 값이지만 현재 schema만으로는 통과할 수 있습니다.

attribute policy validator와 영구적인 chunk별 재시도 이력 저장은 후속 이슈에서 다룹니다. 현재 재시도 로그는 job/chunk ID, attempt, 안전한 reason과 반복 횟수만 포함합니다.

세계관 추출·대상 탐색·비교는 `app/analysis/json_response.py`의 공통 JSON/Pydantic 검증 재시도를 사용합니다. `S*`/`T*` ref 범위, 최대 대상 수, UPDATE/MERGE의 실제 속성명, ADD/EXCLUDE의 추출값 보존 규칙은 provider 응답 뒤 Python에서 추가 검증합니다. 실제 UUID와 version은 prompt에 포함하지 않습니다.

## 현재 subject fallback 방식

`CharacterSubjectResolver`는 설정 후보를 다시 추출하지 않고, 이미 추출된 후보 중 `entity_name`이 비어 있거나 placeholder/지칭어처럼 구체적이지 않은 후보를 대상으로 LLM을 추가 호출합니다. `raw_entity_mention`의 형태는 진입 조건으로 사용하지 않습니다.

- 호출 단위는 current chunk 기준 batch입니다.
- 같은 current chunk에서 나온 fallback 후보는 한 번의 호출로 묶습니다.
- 입력 문맥은 previous/current/next chunk로 제한합니다.
- 응답은 후보별 `resolved_entity_name`만 받습니다.
- `resolved_entity_name`이 null이거나 placeholder/지칭어이면 정상적인 해소 실패로 보고 원래 후보를 `entity_name="미상"`으로 보존합니다.
- `MATCHED`, `UNRESOLVED`, `AMBIGUOUS` 같은 최종 매칭 상태는 Python의 `character_name_resolver`가 계산합니다.
- 보존된 `미상` 후보는 최종적으로 `AMBIGUOUS`가 되며, malformed 응답이나 candidate ID 누락·중복·추가는 기술적 계약 오류로 분석 실패 처리합니다.
