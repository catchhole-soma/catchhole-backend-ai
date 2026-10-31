# 검수 실행과 호출형 검사 (#306/#309/#310)

`EpisodeValidationWorker`는 실제 Java ValidationJob과 leased HTTP를 사용해 설정/오탈자를 독립 실행한다.
`SettingValidationService(...).inspect()`와 `check_typos(...)`의 기존 callable API도 유지한다.
전용 실행/기본 OFF·명시 예산/복원/원장/수동 검증은 [실행 계약](../../docs/validation-execution-v1.md)을 따른다.
Worker는 서버 projectionVersion/modelSettings/modelSourceCatalogue와 clueVersion/modelClues만 사용하며
로컬 projection/CL fallback은 Job 없는 callable에만 남긴다. authorCorrectionProofs는 실제 편집 transport이며
canonical author_correction=null을 자유 문자열 서사 시점·조건의 권위로 바꾸지 않는다.

PLANNER/선정 전체 page/COVERAGE/각 BOOK_SOURCE/BOOK_COVERAGE를 즉시 저장하고 MERGED_PLAN의
서버 check UUID/revision/digest로 판정한다. 성공 NO_CONFLICT와 오탈자 segment도 즉시 저장한다.
같은 입력의 crash/reclaim은 성공 단계·판정·구간을 재호출하지 않고, FOLLOW_UP은 영향 revision을 원자 변경한다.
첫 책 검토를 저장한 뒤 다음 책을 읽고 재개 시 미완료 rule만 source 범위로 읽는다.
전체 책이나 저장 stage/decision을 checkpoint에 중복 복사하지 않는다. 원문 교체·파기는 모든 사본을 지운다.

두 분기는 한쪽 실패·질문0으로 취소하지 않는다. 역할 cap/예약 실패와 공유 lease/input/source 손실을 구분하며,
공유 무효화 때 양쪽의 새 예약·게시를 막고 기존 실제 사용량은 UNKNOWN/늦은 SETTLE로 정산한다.
`await worker.aclose()`는 등록된 claim·초기화·두 분기 실행을 멈춘 뒤 해당 실행과 원장 정리를 모두 기다리고 HTTP를 닫는다.
취소 뒤 분리된 예약 응답·UNKNOWN·SETTLE도 검수 ledger가 소유하며, 이미 측정한 실제 사용량을 유지한다.
동시·반복 close는 한 번의 종료를 공유하고 한 호출자의 취소가 나머지 정산을 중단하지 않는다.
새 예약·provider 호출·게시를 시작하지 않으며 원래 API timeout/cancellation 계약을 사용한다.
SELECT 전용 session factory를 명시하면 기존 frozen RAG를 사용한다. optional embedding은 실제 index와
model/version/1536 차원·명시 cap이 일치할 때만 실행하고, exact query/input/manifest/index proof가
원 호출 원장에 맞는 성공 VECTOR receipt를 재사용한다. 미준비 vector는 정상 빈 결과가 아니다.
VECTOR는 Job 시작 때 읽은 readiness가 완전하고 query embedding client가 있을 때만 모델에 제안한다(GH344).
같은 Job의 이전 시도가 VECTOR를 저장했으면 재개 때는 그대로 유지한다.
claim에 QUERY_EMBEDDING 역할이 아예 없으면 embedding 없이(키워드만) 실행하고, 있는데 index와 다르면 계속 거절한다.

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

async def inspect(context: ValidationContext, base_url, internal_key, llm, budget):
    # context/인증값은 신뢰 서버 실행에서 고정한다. 모델 출력으로 만들지 않는다.
    data = ValidationDataClient(base_url, internal_key, context)
    try:
        result = await check_typos(data, llm, budget=budget)
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
`input_tokens`와 `output_tokens`는 각각 한 요청의 실제 입력 상한과 생성 출력 상한이다.
`segment_chars/context_chars`는 공통 `split_segments`에 그대로 전달한다.
나머지 공통 catalogue/search 예산은 오탈자 분기에서 사용하지 않는다.
count와 생성, 원문 읽기와 최종 검증은 같은 `seconds` 벽시계 예산 안에서 실행한다.
운영 기본값·품질 합격선은 이 모듈에서 정하지 않는다.

## 제안과 위치

- 모델은 `gpt-6.1-terra`, 호출별 옵션은 불변 `LlmCallOptions(reasoning_effort="low")`다.
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
