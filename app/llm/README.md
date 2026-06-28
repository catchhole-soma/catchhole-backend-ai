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

## 현재 추출 방식

현재 단계에서는 prompt로 JSON 응답을 요구하고, Python schema로 결과를 검증합니다.

LLM JSON 검증 실패 시 재시도하거나, OpenAI Structured Outputs의 JSON schema 강제를 적용하는 작업은 후속 이슈에서 다룹니다.
