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
  - `LLM_API_KEY`, `LLM_MODEL`, `OPENAI_RESPONSES_API_URL` 설정을 사용합니다.
  - 응답 텍스트와 token usage를 `LlmTextResponse`로 반환합니다.
- `responses.py`
  - LLM 호출 결과를 내부에서 전달하기 위한 `dataclass` 값 객체를 둡니다.
- `prompts/character_setting_extraction.md`
  - 캐릭터 중심 설정 후보 추출 prompt입니다.
- `prompts/character_subject_resolution.md`
  - 지칭어/placeholder 후보의 주체만 해소하는 fallback prompt입니다.

## 토큰 사용량 상태

`OpenAIResponsesClient`는 OpenAI 응답의 `usage.input_tokens`, `usage.output_tokens`를 읽어 `LlmTextResponse`에 담습니다. 여기까지는 구현되어 있지만, 현재 설정 추출 흐름은 다음 단계에서 token usage를 전달하지 않습니다.

```text
OpenAIResponsesClient
-> LlmTextResponse(input_token_count, output_token_count)
-> CharacterSettingExtractor / CharacterSubjectResolver에서 응답 text만 사용
-> WorkerRunSummary에는 합산되지 않음
-> Spring complete 요청의 inputTokenCount / outputTokenCount는 None
```

따라서 현재 `analysis_jobs.input_token_count`, `output_token_count`에는 설정 추출 LLM 사용량이 저장되지 않습니다. 후속 토큰 집계 작업에서는 다음 호출을 모두 합산해야 합니다.

- 청크별 설정 후보 추출 호출
- JSON 파싱 또는 schema 검증 실패로 다시 호출한 재시도
- 지칭어/placeholder 후보를 처리하는 subject fallback 호출
- 임베딩 호출의 입력 토큰

성공한 마지막 호출만이 아니라 실제 비용이 발생한 재시도까지 포함해야 합니다. 분석 작업이 최종 실패한 경우에는 현재 Spring fail API에 토큰 필드가 없으므로, 실패 작업의 사용량 보존 범위와 API 계약도 함께 결정해야 합니다.

## 현재 추출 방식

현재 단계에서는 prompt로 JSON 응답을 요구하고, Python schema로 결과를 검증합니다.

JSON 파싱 실패 또는 Python schema 검증 실패는 `CharacterSettingExtractor`에서 재시도합니다. 다만 프롬프트 정책 위반까지 강제하지는 않습니다.

예를 들어 `attribute_name`이 `items`처럼 suffix 없이 오거나, `confidence`가 `0.0`인 응답은 프롬프트상 원하지 않는 값이지만 현재 schema만으로는 통과할 수 있습니다.

OpenAI Structured Outputs의 JSON schema 강제, attribute policy validator, chunk별 재시도 이력 기록은 후속 이슈에서 다룹니다.

## 현재 subject fallback 방식

`CharacterSubjectResolver`는 설정 후보를 다시 추출하지 않고, 이미 추출된 후보 중 지칭어 + placeholder 후보만 대상으로 LLM을 추가 호출합니다.

- 호출 단위는 current chunk 기준 batch입니다.
- 같은 current chunk에서 나온 fallback 후보는 한 번의 호출로 묶습니다.
- 입력 문맥은 previous/current/next chunk로 제한합니다.
- 응답은 후보별 `resolved_entity_name`만 받습니다.
- `resolved_entity_name`이 null이거나 placeholder/지칭어이면 해소 실패로 보고 저장 후보에서 제외합니다.
- `MATCHED`, `UNRESOLVED`, `AMBIGUOUS` 같은 최종 매칭 상태는 Python의 `character_name_resolver`가 계산합니다.
