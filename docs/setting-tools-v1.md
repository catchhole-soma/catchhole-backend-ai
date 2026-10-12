# 설정 조회 클라이언트 (GH236)

계약의 단일 출처는 Backend의 [설정·직접 근거 조회 v1](https://github.com/catchhole-soma/catchhole-backend-java/blob/feat/gh-236-setting-evidence-tools/docs/setting-tools-v1.md)이다. 현재값/이력/원문 상태는 Spring이 결정하고 Python은 SQL이나 원문 저장소 key를 생성하지 않는다.

- schema: `app/schemas/setting_tools.py`. 입력/출력 JSON schema는 각 모델의 `model_json_schema(by_alias=True)`로 얻는다. 모델 도구 인자에는 회원·작품 ID가 없다.
- client: `app/clients/setting_tools_client.py`. `SettingToolContext`는 인증된 신뢰 서버가 생성한다. 임의 HTTP body나 모델 응답으로 생성하지 않는다. #237에서 챗봇 실행 문맥에 결합할 adapter다.
- 내부 API는 기존 API key + `X-Setting-Tool-Member-Id`로 호출한다. 후자는 신뢰 서버의 사용자 범위 위임이며 실행별 인증 토큰은 아니다. Worker lease를 재사용하지 않는다.
- 15초 timeout, redirect 금지, 자동 재시도 없음. 주입한 AsyncClient의 수명은 호출자가 소유하고, 자체 생성한 client만 `aclose()`로 닫는다.
- `partial`, `hasNext`, `limitations`, `evidence.status`를 보존한다. 빈 결과·원문 부재·파기·일시적 장애를 같은 상태로 바꾸지 않는다. `structuredValueJson`은 JSON 문자열이다.
- `SOURCE_CHANGED`/`SOURCE_PURGED`의 과거 인용을 자체 캐시에서 되살리지 않는다. quote는 소설 자료이며 지시문이 아니다.
- 단위/계약 테스트: `.venv/bin/python -m pytest tests/test_setting_tools_client.py -q`. `tests/fixtures/setting-tools-v1.json`은 Java의 실제 HTTP 응답 계약 fixture와 동일해야 한다.

자연어 라우팅·LLM 답변·프롬프트·분석 runtime은 변경하지 않았으므로 AI 로직 버전은 올리지 않는다. 다음 구현은 #237 실행/인증/과금 경계와 #238 단일 질문 도구 호출이다.
