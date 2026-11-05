# 검수 실행과 호출형 검사 (#306/#309/#310, grid v4 #349 개편)

`EpisodeValidationWorker`는 실제 Java ValidationJob과 leased HTTP를 사용해 설정/오탈자를 독립 실행한다.
`SettingValidationService(...).inspect()`와 `check_typos(...)`의 callable API도 유지한다.
전용 실행/기본 OFF·명시 예산/복원/원장/수동 검증은 [실행 계약](../../docs/validation-execution-v1.md)을 따른다.
Worker는 서버 `projectionVersion=2`(SUBJECTS/SETTINGS, modelClues 없음, 현재값만)와 `configurationVersion=2`(역할별 모델·추론 강도·출력 상한)만 받는다.
Job 없는 callable과 실험 harness는 같은 projection을 `projection.py`의 Python 대응으로 만든다. authorCorrectionProofs는 실제 편집 transport이며
canonical author_correction=null을 자유 문자열 서사 시점·조건의 권위로 바꾸지 않는다.

모듈:
- `contracts.py`: 계약·`split_setting_segments`(grid v4)·`split_segments`(TYPO)·판정 답 `JudgeOpenAnswer`/`JudgeFinalAnswer`·예산.
- `planning.py`: 구간 PLANNER 병렬 호출·정규화·게시·MERGED_PLAN 병합·서버 grid 대조.
- `judging.py`: 판정 상태 기계(OPEN → 검색·문맥 → FINAL, 충돌 의심 변화 검색, 최대 3회)·`SettingsCache`·판정 검증.
  판정 규칙 보완(2026-11-03): 충돌(CONFLICT·SUSPECTED_CONFLICT)은 대상 문장과 직접 어긋나는 이전 문장이 있을 때만이고, 그 문장은
  `basis_quotes`에 글자 그대로 옮겨야 한다. 확인된 근거 문장이 하나도 없는 충돌 판정은 저장 전에 NO_CONFLICT로 바꾼다
  (`clear_unquoted_finding`, `CONFLICT_QUOTE_MISSING_CLEARED`/`SUSPECTED_CONFLICT_QUOTE_MISSING_CLEARED`, 질문 실패 아님,
  DB·원문 불일치(`disagreements`)가 있는 판정은 기존 NEEDS_CONTEXT 규칙 그대로). 검색을 요청한 FOLLOW_UP마다 Worker가 질문의
  대상 문장과 앞뒤 100자(줄 단위로 맞춤)로 만든 VECTOR 검색 하나를 판정 검색 뒤에 더 한다(`target_search_query`, 판정의 3개와 별도,
  `TARGET_SEARCH_INSERTED`). 충돌 의심 변화 검색 문장은 `check_intent` + 대상 문장이다(`change_search_query`).
- `author_text.py`: 작가가 읽는 판정 문장(`headline`·`reason`·`impact_reason`·`disagreements[].reason`)의 내부 참조를 "이번 원고"·"N화"·"저장된 설정"·대상 이름으로 바꾸고 모르는 참조·내부 용어 문장은 버리는 저장 전 안전망(`AUTHOR_TEXT_SANITIZED`). 판정 근거 문장 `basis_quotes`는 `judging.normalize_basis_quotes`가 evidence 조각(절대 위치 인용)·저장 설정 값(부분 문자열)으로 확인하고 틀린 것은 버린다(`BASIS_QUOTES_DROPPED`, 질문 실패 아님). 판정 작성 제목 `headline`(2026-11-03)은 고정 대체 문장 없이 정제하고, 비거나 80 code point를 넘거나 NO_CONFLICT면 None으로 게시한다(`HEADLINE_DROPPED`·`HEADLINE_MISSING`·`NO_CONFLICT_HEADLINE_CLEARED`, 판정 입력 `target_episode_no`).
- `corpus.py`: L receipt·E-ref 발급과 실행 단위 RAG 재사용(lock 하나, 같은 문장 1회 검색). 범위는 앞 회차와 현재 판 설정집.
- `search.py`: report fence가 있는 frozen readiness와 회차·설정집 조각을 한 순위로 고르는 VECTOR(`frozen_vector`), 범위 `indexed_sources`. fence는 report의 소유권·현재성·`baseline_hash`·READY·원고 fingerprint와 출처 manifest를 보며, #381부터 seed·entries 열은 요구하지 않는다(서버가 확정 설정을 복사하지 않고 현재 설정을 읽는다).
- `index_fill.py`: 접수 때 색인 채우기(2026-11-02 결정) — 범위의 빈 곳만 READ_SOURCE 원문으로 다시 자르고 READY가 아닌 조각만 임베딩.
- `failures.py`: 질문 단위/실행 단위 실패 분류와 정책 스위치 `CONTINUE_AFTER_QUESTION_FAILURE`(결정 ③: 운영도 실패한 질문만 INCOMPLETE로 게시하고 나머지를 계속 판정, 실험은 게시 없이 계속).
- `service.py`: 조율(입력 읽기 → 색인 채우기와 계획 동시 → 채우기 대기·readiness → 판정 4개씩).
- `models.py`/`schemas.py`: `RoleRuntime`, PLANNER/JUDGE 지시문, `call_role`, v3 strict schema.
- `execution.py`: claim 예산·역할 runtime(`claim_roles`), 역할별 metered client, query embedding(lock), 색인 채우기 embedding(`INDEX_EMBEDDING`, `indexInput`).

PLANNER 단계를 검증 즉시 저장하고 서버 v4 grid를 확인한 뒤 MERGED_PLAN의 서버 check UUID/revision/digest로 판정한다.
결정된 질문과 오탈자 segment도 즉시 저장한다. 같은 입력의 crash/reclaim은 저장된 PLANNER·결정·구간을 재호출하지 않고,
게시되지 않은 질문만 첫 판정 호출부터 다시 한다(JUDGING runtime checkpoint·`/revisions` 없음). 원문 교체·파기는 모든 사본을 지운다.

두 분기는 한쪽 실패·질문0으로 취소하지 않는다. 역할 cap/예약 실패와 공유 lease/input/source 손실을 구분하며,
공유 무효화 때 양쪽의 새 예약·게시를 막고 기존 실제 사용량은 UNKNOWN/늦은 SETTLE로 정산한다.
`await worker.aclose()`는 등록된 claim·초기화·두 분기 실행을 멈춘 뒤 해당 실행과 원장 정리를 모두 기다리고 HTTP를 닫는다.
취소 뒤 분리된 예약 응답·UNKNOWN·SETTLE도 검수 ledger가 소유하며, 이미 측정한 실제 사용량을 유지한다.
동시·반복 close는 한 번의 종료를 공유하고 한 호출자의 취소가 나머지 정산을 중단하지 않는다.
SELECT 전용 session factory를 명시하면 frozen RAG를 사용한다. optional embedding은 실제 index와
model/version/1536 차원·명시 cap이 일치할 때만 실행한다. 같은 실행 안의 같은 검색 문장은 한 번만 embedding·검색하고
결과를 재사용한다(`reused_from`). 실행 사이(retry)의 재사용은 없다. 미준비 vector는 정상 빈 결과가 아니다.
검색은 판정 직전에 읽은 readiness가 범위(앞 회차·현재 판 설정집) 전체에서 완전하고 query embedding client가 있을 때만 판정에 제안한다(GH344).
readiness 읽기는 최대 5초이고, 실패·초과하면 그 Job은 검색 없이 진행하며 변화 검색이 필요한 질문은 `VECTOR_NOT_READY`다.
범위가 비면 검색은 근거 없이 성공한다(`NOT_APPLICABLE`).
접수 때 색인 채우기: 쓰기 session factory(`index_sessions`, runner `--index-url-env`)와 claim의 INDEX_EMBEDDING 역할이 있으면 INPUT 직후 계획과
동시에 범위의 빈 곳만 채우고(이미 READY인 조각은 다시 임베딩하지 않음, 바뀐 원고는 다시 자름) 판정은 그 뒤에 시작한다. 채우기 실패(provider·cap·DB·
원문 문제)는 실행을 멈추지 않고 검색이 필요한 질문만 `VECTOR_INDEX_FILL_FAILED`(원인 `INDEX_FILL_*`)로 남긴다. 비용은 서비스 부담이다.
설정집 조각 근거는 `source_kind=SETTING_BOOK` E ref이며 판정 프롬프트는 회차가 아닌 설정집으로 표시한다. claim에 QUERY_EMBEDDING 역할이 아예 없으면 embedding 없이 실행하고,
있는데 index와 다르면 계속 거절한다.

기본 추출·챗봇 모델/재시도, 추출 Job·후보·자동 확정/철회는 바꾸지 않는다. #312/#314는 별도 범위다.

## 독립 오탈자 검사 (#309)

`await check_typos(data, llm, budget=budget)`는 인증된 `ValidationDataClient`에서 같은
고정 원고를 읽고, 전체 핵심 구간을 검사한 `ClassifiedTypoCheckResult`를 반환한다.
설정 질문·기준 항목·설정 검사 결과를 인자로 요구하거나 조회하지 않는다.
`INPUT → READ_SOURCE → 구간별 count/generate → VALIDATE`의 읽기 전용 경로다.

2026-10-29 통합 기준은 #306 AI PR #99가 머지된 main `22ea1dc`다.
공통 reader/계약/호출 옵션은 main에서 소비하며 #309이 공통 v2 patch를 다시 덮어쓰지 않는다.
설정 검사 흐름과 공유 경계는 [설정 검수 계약](../../docs/setting-validation-v1.md)을 따른다.

```python
from app.validation.data import ValidationContext, ValidationDataClient
from app.validation.typo import check_typos

async def inspect(context: ValidationContext, base_url, internal_key, llm, budget, role):
    # context/인증값은 신뢰 서버 실행에서 고정한다. 모델 출력으로 만들지 않는다.
    # role은 claim configuration v2의 TYPO RoleRuntime(model, 추론 강도, 출력 상한)이다.
    data = ValidationDataClient(base_url, internal_key, context)
    try:
        result = await check_typos(data, llm, budget=budget, role=role)
        return result.model_dump(mode="json")  # category를 포함한 실제 subtype으로 직렬화
    finally:
        await data.aclose()
```

`llm`에는 정확한 기존 `OpenAIResponsesClient` 타입 또는 그것을 직접 한 번만 감싼
정확한 기존 `MeteredTextGenerationClient(max_retries=0)` 타입을 주입한다.
기본 재시도 wrapper, subclass, 알 수 없는 proxy, 중첩/순환 delegate는 reader/provider/원장
호출 전에 `LLM_CLIENT_CONFIGURATION_INVALID`로 거절한다. 숨은 재시도와 이중 계량 때문에
model_calls 상한을 우회하지 않게 하기 위함이다. 호출 전 전용 wrapper를 max_retries=0으로
만들며 공유 인스턴스/전역 기본값을 실행 중 바꾸지 않는다.
이 허용을 위해 검수에 가짜 AnalysisJob/lease를 만들지 않는다. 검수 실행·계량은 전용 #310 Worker가 실제 ValidationJob 원장 adapter로 연결한다.
호출자는 `InvocationBudget`의 모든 수치를 명시한다.
`model_calls`는 생성 시도 수, `lookups`는 frozen reader 요청 수이며 정상 경로는 3회다.
`input_tokens`는 한 요청의 실제 입력 상한이고, 생성 출력 상한은 `role.max_output_tokens`다.
`segment_chars/context_chars`는 공통 `split_segments`에 그대로 전달한다.
나머지 공통 catalogue/search 예산은 오탈자 분기에서 사용하지 않는다.
count와 생성, 원문 읽기와 최종 검증은 같은 `seconds` 벽시계 예산 안에서 실행한다.
운영 기본값·품질 합격선은 이 모듈에서 정하지 않는다.

## 제안과 위치

- 모델·호출별 옵션은 claim configuration v2의 TYPO `RoleRuntime`(운영 권장 `gpt-6.1-terra`, `low`)이다.
  count와 생성에 동일한 system/user/model/schema/options를 제공한다.
  추출·챗봇의 전역 기본값이나 공유 client 속성은 바꾸지 않는다.
- 공통 `TypoSuggestion`에는 분류가 없어서 #309-local subtype에만 `category`를 추가했다.
  `SPELLING`은 맞춤법, `SPACING`은 띄어쓰기, `TYPO`는 명백한 문자/조사 중복·누락이다.
  main의 공통 파일과 기존 schema는 수정하지 않는다. 공통 타입의 직접 재파싱은 category를 거절하므로
  후속 저장 DTO는 실제 subtype의 전체 직렬화를 소비하도록 연결해야 한다.
- `Span`은 `T1`의 Unicode code point `[start,end)`와 실제 slice의 exact quote다.
  같은 문장은 위치로 구별한다. 제안의 start가 핵심 구간 안에 있을 때 그 구간이 소유한다.
  GH331: 모델 위치가 1~4 code point 어긋나도 quote가 정확하면, 그 구간이 소유할 수 있는 가장 가까운 정확한 위치로 다시 계산한다.
  원고에 없는 quote와 같은 거리의 반복은 그대로 `INVALID_EXACT_SPAN`이다. 설정 검사 span·판정 규칙은 [설정 검수 계약](../../docs/setting-validation-v1.md)의 GH331 절을 따른다.
  제안 끝은 제공된 문맥 안에서 핵심 구간 경계를 넘을 수 있다.
- 구간별 응답 전체를 schema·coverage·원문 위치·중복/겹침·빈 이유·무변경 제안으로 검증한다.
  실패한 batch의 일부 제안을 건지지 않는다. 명백한 불필요 문자의 삭제는 빈 replacement로 표현할 수 있다.
- 문체·고유명사·방언·의도한 반복 보존은 전용 prompt로 지시한다. 구조 검증만으로 의미 품질을
  보증하지 않으며, 대역 응답을 쓴 테스트는 실제 모델 정확도 평가가 아니다.
- 내부 회원/작품/리포트/저장소 ID·hash·key/version·인증값은 모델에 투영하지 않는다.
  원문 내부 명령은 JSON 문자열에 담긴 비신뢰 소설 자료다.

`됬다.`의 결과 예시(원문은 그대로):

```json
{
  "target_hash": "5b7b380908e3890e4a519ab7ee1e3775464da28dfb13323666eb51899cf9635c",
  "status": "COMPLETED",
  "segments": [{"segment_ref": "TSEG1", "status": "PLANNED", "reason": "전체 핵심 구간을 검사했습니다."}],
  "suggestions": [{
    "span": {"source_ref": "T1", "start": 0, "end": 2, "quote": "됬다"},
    "replacement": "됐다", "reason": "잘못된 맞춤법을 바로잡습니다.", "category": "SPELLING"
  }],
  "failure_code": null
}
```

## 완료와 실패

공통 coverage 이름을 유지한다. `PLANNED`는 검증된 제안이 있는 전체 검사 완료 구간,
`NO_CHECK`는 전체 검사 후 제안이 없는 구간, `UNPROCESSED`는 미완료/무효 구간이다.
`COMPLETED`는 모든 핵심 구간과 최종 frozen 검증이 성공한 경우뿐이다. 빈 원고도 읽기와
최종 검증을 수행하며 구간·제안 배열이 빈 완료 결과가 된다.

독립 provider/형식/위치 실패는 해당 구간만 미완료로 남기고 다음 구간을 검사한다.
검증된 다른 구간 제안은 `INCOMPLETE` 결과에 보존한다. 거절된 provider 요청(4xx),
예산 소진, 예상 밖 I/O 오류는 나머지 구간을 중단한다. 자동 재시도는 추가하지 않는다.
`failure_code`는 최초 구간 실패이고 각 미완료 구간의 `reason`에도 고정 오류 코드를 둔다.
출력 절단은 `LLM_OUTPUT_TRUNCATED`, 비완료 응답은 `LLM_INCOMPLETE_RESPONSE`,
형식/허용 필드 오류는 `TYPO_RESPONSE_INVALID`다. count 실패는 `INPUT_TOKEN_COUNT_FAILED`로
생성 없이 종료하며 입력 초과는 `INPUT_BUDGET_EXCEEDED`다. 원문/입력 코드는 공통 reader를 따른다.

최종 `validate()`가 실패하거나 예산 때문에 수행되지 못하면 이전 제안을 모두 비우고 모든
알려진 구간을 `UNPROCESSED`로 바꾼다. 최종 오류 코드가 앞선 구간 실패보다 우선한다.
전문을 읽지 못하면 구간을 계획할 수 없어 구간 배열도 비지만 상태는 `INCOMPLETE`다.
시간 만료로 최종 재검증을 못 한 결과도 이전 제안을 유효하다고 반환하지 않는다.
호출자 취소는 전파하며 원문·prompt·응답·예외 원문을 로그에 추가하지 않는다.

원고 자동수정·예외 사전·작가 판단·설정 추출은 구현하지 않는다. 실제 Worker/lease·병렬 실행·
원장/영속 저장 및 저장 직전 재검증은 #310, 판단/추출 허가는 #312, 화면은 #314에서 연결한다.

## 최신 main 통합 검증 — 2026-10-29

`python -m pytest tests/test_typo_checker.py -q`는 HTTP 대역 기반 신규 59개 검사다.
설정 검사·공통 계약·metering·선택적 PG 검사를 포함한 집중 회귀는 132 passed / 6 skipped,
전체는 4,608 passed / 140 skipped / 기존 경고 1개다.
선택적 GH306 PG 6개를 포함한 DB/Java export/Docker 조건 skip이며 신규 오탈자 테스트 skip은 없다.
신규 Python 2파일 ruff는 통과했고 전체 ruff는 main과 동일한 기존 480건으로 증감이 없다.
checker·prompt·59개 테스트는 독립 검토를 마친 버전을 그대로 통합했다.
별도 격리 build 환경에서 wheel 생성과 `typo.py`/전용 prompt 포함·원본 바이트 일치를 확인했다.
프로젝트 의존성·공유 venv는 변경하지 않았다.

별도 실제 gpt-6.1-terra/low 합성 3사례는 R1 주입 경계 수정 전 snapshot에서
기본 교정 3제안·이름/말투 대조 0제안·
emoji/반복 두 위치를 관측했다. reader는 대역이며 provider만 실제 호출했다.
R1 및 최신 main 통합 후 추가 유료 호출은 하지 않았다. 소수 사례를 전체 정확도로 집계하지 않는다. 조건/usage/복원 기준은
[v0027 상세](../../docs/ai-logic-versions/details/v0027.md)에 기록했다.
