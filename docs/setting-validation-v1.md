# 설정 충돌 검사 호출 계약 v1 — GH306

2026-10-28. Java/AI 저장소의 동일 문서다. 검수 Worker·lease·결과 저장은 GH310,
오탈자 실행은 GH309, 작가 검토·추출 허가는 GH312, 화면은 GH314다.
이 구현은 설정/원문을 읽는 호출형 서비스이며 추출 Job·후보·새 대상·현재값·검토 완료 기록을 쓰지 않는다.
자동 실행, 운영 기능 활성화, 실제 모델 품질/비용 측정이나 배포를 포함하지 않는다.

## 소유권과 입력

신뢰 서버가 `ValidationContext(member_id, work_id, report_id, baseline_hash, target_hash)`를 만든다.
서버 ID·SQL·저장소 key·lease·작품/기준 선택 권한을 모델에서 받지 않는다.
현재 GH303에는 별도 검수 Job/lease가 없으므로 report가 고정 기준을 식별하며 baselineRevision은 1이다.
별도 기준 revision 저장이 도입되면 계약 버전을 올려 연결한다. 현재 값을 과거 기준으로 재구성하지 않는다.

`ValidationDataClient`는 다음 내부 API를 호출한다.

`SourceDocument`의 핵심 필드는 frozen이지만 metadata dict는 깊게 불변이 아니다. 소비자는 생성 후 metadata를 읽기 전용으로 취급한다. 내부 dict까지 불변으로 만드는 정리는 비차단 Minor M1로 남기며 현재 실행 코드에는 metadata를 변경하는 경로가 없다.

```text
POST /api/internal/v1/works/{workId}/validation-reports/{reportId}/data
X-Internal-Api-Key: <서버 비밀값>
X-Validation-Member-Id: <인증된 실행의 회원>
```

공통 요청: `expectedBaselineHash`, `expectedTargetHash`, `operation`, `offset >= 0`, `1 <= limit <= 100`, 선택 `subjectRefs`.
`INPUT`은 고정 target·허용 sources·entryCount, `SUBJECTS`는 주체 카탈로그(주체당 한 행), `SETTINGS`는 요청 주체(최대 100)의
현재값 행(`characters`·`worldSettings`만, `worldHistory` 제외)을 반환한다. `ENTRIES`(저장된 baseline_entries 페이지)는 legacy로 남는다.
`READ_SOURCE`는 `sourceRef`, `expectedSourceHash`, `expectedSourceVersion`, 선택적 `start/end`를 받는다.
`VALIDATE`는 report/target/허용 출처의 현재 접근·원본 metadata를 다시 확인한다.
응답은 기존 `CommonResponse.data` 안에 formatVersion/reportId/baselineRevision/baselineHash/targetHash/manifestHash/status를 갖는다.

대상은 `T1`, 과거 회차/설정집은 `R1...`, 고정 항목은 `B1...`, 모델용 대상은 `S1...`, 실제 읽은 구간은 `E1...`다.
내부 JSON은 source IDs/key/hash/version을 보존하지만 모델 입력에서는 짧은 참조와 읽을 값/시점/조건만 제공한다.
Fact 출처의 ID/hash가 manifest와 맞을 때만 원문 참조로 연결한다. 제공되지 않은 대상/설정 후보를 만들지 않는다.

## 실제 원문과 검색

원문 위치는 Unicode code point `[start,end)`다. Python slice와 동일하고 Java는 code point↔UTF-16 변환을 사용한다.
반복 구절도 정확한 위치로 구분하며 인용을 다른 위치로 옮기거나 존재하지 않는 문장으로 보정하지 않는다.
예외(GH331): 모델이 쓴 대상 원고 span은 검증 전에 Worker가 정규화한다. 대상 segment 별칭 `TSEGn`은 `T1`으로 바꾸고,
quote가 정확하지 않은 위치에 있으면 **모델 자신의 quote**가 정확히 나타나는 위치 중 모델 start에 가장 가까운 곳으로 start/end를 다시 계산한다.
후보는 해당 구간 범위(설정: 구간 문맥, 오탈자: 핵심 구간 start·문맥 end) 안으로 제한한다. 이미 정확한 span은 옮기지 않으며
원고에 없는 quote, 같은 거리의 반복(모호), 알 수 없는 참조(`T2`, `R1` 등)는 바꾸지 않고 기존처럼 `INVALID_EXACT_SPAN`으로 거절한다.
기본 전문 읽기는 반드시 0..totalCodePoints이고 전체 실제 UTF-8 SHA-256을 확인한다.
명시 부분 읽기는 요청과 응답의 범위가 같아야 한다. malformed envelope/text/position은 typed 미완료다.

서버는 읽기 전후 JPA scalar 조회로 회원/작품/report·원문 fingerprint와 파기 상태를 확인한다.
실제 저장 객체를 읽고 hash를 확인하며 최신 원문으로 fallback하지 않는다. 기존 읽기 지원 범위인 UTF-8 2,000,000 byte를 초과하면 실패한다.
기존 ObjectStorage.getText는 응답 VersionId/전송 중 byte 제한을 제공하지 않는다. 고유 revision key·DB version·실제 전체 hash를 검증하는 경계이며 이 한계를 숨기지 않는다.
직접 입력 설정은 원래 원문이 없을 수 있고 그 사실만으로 실패하지 않는다. HUMAN/자동 확정/수동 보호 표시를 명시 정정으로 추정하지 않는다.

`FrozenRetrievalRepository`는 DB report와 validation_baseline_sources를 검색의 공통 SQL 조건에 결합한다.
회원/작품/수명·target·정확한 허용 출처 hash/key/version·대상 이전 회차 필터를 순위/LIMIT 전에 적용한다.
`FrozenRetrievalSearchService`는 호출자가 명시적으로 주입한 읽기 session factory만 사용하며 기본 DB/embedding provider를 만들지 않는다.
grid v4의 검색은 VECTOR 하나다. 벡터 호출에는 같은 index spec의 계량 embedding client를 주입한다.

`ValidationCorpus`(grid v4)는 실행 단위 RAG 재사용 장치다. L receipt와 E-ref를 발급하고, 같은 검색 문장(앞뒤 공백 제외)은
한 실행 안에서 한 번만 실제로 검색한다(조회 1회, embedding 1회). 재사용한 결과는 새 L-ref에 `reused_from`을 단다.
embedding·검색·proof 기록 전체를 실행당 lock 하나로 직렬화하므로 동시에 같은 문장을 묻는 두 판정도 검색은 한 번이다.
키워드·직접·인접 읽기, 미색인 회차/설정집 원문 읽기, 조각마다 저장소를 다시 읽는 확인은 없다. 인용 검증은 게시 때 서버가 한다.
과거 회차 출처가 없으면 VECTOR는 근거 없이 성공하고(`vector_status=NOT_APPLICABLE`) embedding을 만들지 않는다.
회차 출처가 있는데 색인이 완전하지 않으면 검색을 제안하지 않으며, 필요한 변화 검색은 `VECTOR_NOT_READY`로 그 질문을 끝낸다.
검색 시점 readiness가 허용 회차 전체를 덮지 못하면 실패 receipt에 readiness를 남기고 `VECTOR_INDEX_PARTIAL`이다.
설정집은 RAG 색인이 생길 때까지 직접 참조하지 않는다(#349 결정).

## 질문·판정 (grid v4, #349 개편)

`SettingValidationService(data_client, model_client, budget, roles, search=None, embedding_client=None, checkpoint=None,
experiment=False, judge_concurrency=4).inspect()`가 진입점이다. `roles`는 PLANNER·JUDGE의 `RoleRuntime(model, call_options,
max_output_tokens)`이다. `model_client`는 create_text_response와 exact count_input_tokens를 제공해야 한다(`for_validation_role`이 있으면 역할별 client).
검수 과금/Worker 원장 연결은 GH310의 책임이다. 기존 추출용 MeteredTextGenerationClient를 재사용하기 위해 가짜 AnalysisJob/lease를 생성하지 않는다.

1. INPUT, 실제 target 전문, SUBJECTS를 읽는다. 원고를 `segment-chars`(1만) 기준 구간 수로 같은 길이로 나누고 줄바꿈 뒤에서 자른다(grid v4 분할,
   [실행 계약](validation-execution-v1.md)). 1만 자 이하 원고는 한 구간이다.
2. PLANNER를 모든 구간에 병렬로 호출한다(`app/validation/planning.py`). 입력은 구간, `text[contextStart:contextEnd]`(앞뒤 `context-chars` 1,000자 문맥),
   `text_start`, `target_source_ref: "T1"`, `source_catalogue`, `subject_catalogue`(SUBJECTS 그대로)뿐이다. 설정값은 주지 않는다.
   PLANNER_SYSTEM은 본문의 사실·행동 전제·변화·금지·대상·시점·조건을 빠짐없이(개수를 줄이지 않고) 질문으로 만들게 하고, "나"·"너"·"그" 같은 지칭은 고정 규칙 없이
   그 장면의 앞뒤 문맥으로 판단하게 한다. 문맥은 읽기 전용이며 span은 자기 구간 핵심에만 둔다. 조회 계획(`lookup_intents`)은 없다.
3. 각 구간 결과를 quote 기준으로 정규화(핵심 구간 안에서만 재위치)하고 검증해 즉시 게시한다. 문맥 안 span은 `SPAN_OUTSIDE_SEGMENT`다.
4. 같은 제공 대상·속성/규칙·서사 시점·조건·서술 방식의 질문만 구간 순서로 병합한다(MERGED_PLAN). ID 미해결 질문은 보수적으로 별도 유지한다.
5. 질문 주체의 현재값 행을 SETTINGS로 100개씩 읽어 실행 안에 캐시한다(`SettingsCache`, 중복 요청 합침). `worldHistory` 행과 값 이력은 판정에 보내지 않는다.
6. JUDGE를 4개씩 동시에 실행한다(`app/validation/judging.py`). 질문당 최대 3회: OPEN(DB 현재값과 앞뒤 300자로 판정, 판단할 수 없을 때만 FOLLOW_UP으로
   자연어 검색 문장 최대 3개와 T1 문맥 범위를 요청) → 요청한 검색·문맥을 읽은 뒤 FINAL(DECIDED만) → 충돌 의심이면 Worker 변화 검색 뒤 FINAL.
7. 코드가 실제 제공 참조·정확한 target 위치·성공 receipt·필수 읽기 처리를 검사한다. 모델의 true 네 개를 그대로 완료 조건으로 쓰지 않는다.

판정 입력은 `{question_ref, check_revision, question, target_context, additional_target_context, settings, evidence, receipts, source_catalogue,
input_digest, form}`이다. `form`은 OPEN/FINAL이고, settings는 질문 주체의 압축 현재값 행(아래)이다. 게시 bundle은 같은 키에서 `form`이 없고
settings가 `{entry_ref, subject_ref}` ref 행이다. 판정 schema는 보고서당 OPEN·FINAL 두 개(`validation_judge_open_v3`, `validation_judge_final_v3`)라
provider prompt cache가 유지된다. 계획 schema는 `validation_planner_v3` 하나다. 참조는 종류별 pattern(B/E/L/S/TSEG)으로만 제한하고 정확한 소속은 검증기가 판단한다.
검색을 쓸 수 없으면 OPEN schema에서 `search_queries`가 빠진다. 검색 문장이 3개를 넘으면 `INVALID_FOLLOW_UP`(원인 `INVALID_FOLLOW_UP_TOO_MANY_SEARCHES`),
빈 FOLLOW_UP은 `INVALID_FOLLOW_UP_EMPTY`, 빈·1000자 초과 문장은 `INVALID_SEARCH_REQUEST`, T1 밖 범위는 `INVALID_TARGET_CONTEXT_REQUEST` 원인이다.
FINAL 답이 FOLLOW_UP이면 `MODEL_CONTRACT_INVALID`다.

실패 정책(`app/validation/failures.py` 한 곳): 실행 단위 실패는 새 판정 시작을 멈추고 진행 중 판정은 끝내 게시한다. 질문 단위 실패(모델 양식·잘림·판정 규칙·
FOLLOW_UP 형식·검색 미준비/실패·입력 초과)도 운영에서는 같은 fail-fast이며(정책 재검토 예정), `experiment=True`는 `question_failures[qref]={code,cause,call}`로
기록하고 나머지를 계속 판정한다. `InspectionResult.traces`는 질문별 호출 기록(형식·응답·검색 수·캐시 재사용·변화 검색·오류)이며 게시하지 않는다.

GH331 모델 출력 정규화(실제 Terra 관측 반영, grid v4에서도 유지):

- 대상 span의 `TSEGn`→`T1`, quote 기준 위치 재계산(계획은 자기 핵심 구간 안에서, 판정은 원고 전체에서 가장 가까운 유일한 정확 위치. 없는 quote·동률은 거절).
- strict mode는 root 조건부 필드를 표현할 수 없으므로 판정 조건은 prompt와 검증기가 맡는다. FOLLOW_UP의 outcome/basis/impact/impact_reason/completion은 무시(null)하고
  (`FOLLOW_UP_DECISION_FIELDS_IGNORED`), DECIDED에 붙은 검색·문맥 요청은 실행하지 않고 지운다(`DECIDED_REQUESTS_IGNORED`). NO_CONFLICT의 impact도 null로 둔다.
- `completion.required_reads.receipt_refs`는 Worker가 질문의 실제 성공 receipt 전체로 확정한다. 모델이 인용한 receipt/근거가 실제로 없으면 거절하고, 빠진 receipt만 채운다.
  다른 세 completion 항목은 근거·receipt를 하나 이상 인용해야 한다.
- DECIDED의 `target_spans`는 질문 span 자체다(Backend가 같은 집합을 요구). 원고에 없는 인용은 `INVALID_EXACT_SPAN`으로 거절한다.

결과는 CONFLICT / SUSPECTED_CONFLICT / NEEDS_CONTEXT / NO_CONFLICT이며 실패는 INCOMPLETE로 별도다.
SUSPECTED_CONFLICT/CHANGE_NOT_FOUND는 실제 성공한 변화/예외 VECTOR 검색 receipt가 있어야 한다.
DB 현재값과 확보한 근거가 충분하면 검색을 생략할 수 있으나 대상·시점·조건·변화/예외 설명과 실제 자료 참조가 필요하다.
검색 0건 자체로 직접 모순이나 예외 부재를 증명하지 않는다. 같은 신규 회차만의 모순은 판정하지 않는다.

GH341(grid v4): DECIDED SUSPECTED_CONFLICT가 호출 한도 전에 왔는데 그 질문에 성공한 `CHANGES_EXCEPTIONS` VECTOR receipt가 없으면, Worker가 ref
`Qn-CHANGES_EXCEPTIONS`, 고정 purpose `Worker가 충돌 의심 판정 전에 요구하는 변화·예외 검색`, query = 중복 없는 비어 있지 않은 `subject_mentions` 뒤에
`property_or_rule`(1000자 이내)인 VECTOR 검색 하나를 실행하고 FINAL로 다시 묻는다(`CHANGE_SEARCH_INSERTED`). 세 번째 호출은 이 경로로만 가므로 최종
SUSPECTED_CONFLICT에는 항상 그 receipt가 있다. query를 만들 수 없으면 `INVALID_DECISION_CHANGE_SEARCH_MISSING`, 검색을 쓸 수 없으면 `VECTOR_NOT_READY`다.
다시 받은 판정이 그 receipt를 인용하지 않으면 Worker가 receipt와 알려진 근거(없으면 `T1`)를 덧붙인다(`CHANGE_SEARCH_RECEIPT_FILLED`).
모든 판정은 대상 원고 T1 문맥을 받으므로, `completion.comparison_context.evidence_refs`에 `T1`이 빠지면 Worker가 앞에 덧붙인다(`COMPARISON_CONTEXT_TARGET_FILLED`). 판정·사유·등급은 바꾸지 않는다.

모델 입력 투영(GH349 후속, grid v4에서도 유지): JUDGE의 `settings`는 `app/validation/projection.py`의 `compact_setting_row`로 만든 압축 현재값 행이다.
행은 `entry_ref`, `subject_ref`, `domain`, `name`, `aliases`, `state`(+행에 있으면 `author_correction` 그대로)다. `state`에는 스칼라 값, 스칼라 목록,
`currentNarrativeTime`·`narrativeTime`·`absences`·`protectedSlots`·`protectedPaths`(안의 근거 키 제거), 세계관 값 `propertiesJson`과
`slots`(슬롯마다 `factKey`·`factType`·`factValue`, 스칼라인 `temporalScope`·`effectiveFromEpisodeNo`만)만 남긴다. `historyFacts`·출처·근거·버전은 뺀다.
BASELINE 확장 행(`expanded_setting_row`)과 이력 요청은 없다(#349 결정: DB 이력은 보내지 않고 언제 바뀌었는지는 RAG로 찾는다).
게시·실행 결과의 bundle `settings`는 ref 행만 담는다(GH349 후속 2). `projection.py`는 서버 projection의 Python 대응(`model_settings`, `subject_catalogue`,
`current_settings`, `source_catalogue`, `CURRENT_DOMAINS`)도 두며 Job 없는 호출형·실험 fixture client가 쓰고 공통 fixture 벡터로 서버와 대조한다.

legacy(grid v1~v3): 구간별 SUBJECT_RESOLUTION(RELATED_SELECTION 역할)·COVERAGE(rules/clue_checks)·기계 매칭 `segmentSubjects`·서버 `modelClues`·
설정집 BOOK_SOURCE/BOOK_COVERAGE·키워드/직접/인접 읽기·FOLLOW_UP의 `affected_question_refs` 재개와 `/revisions`·JUDGING checkpoint는 v0043에서 제거했다.
과거 규칙은 git 이력과 v0038~v0042 상세를 따른다.

DB↔원문 불일치는 두 참조를 유지하고 명시 정정의 정확한 대상/항목/시점/조건 범위를 검증한다.
기존 데이터에는 그 scope metadata가 없을 수 있다. 그런 경우 보호/확정 flag를 정정으로 승격하지 않고 NEEDS_CONTEXT로 둔다.
명시 scope가 있는 입력만 해당 범위의 정정을 우선할 수 있다. 이 검사기가 정정 metadata나 원문/DB를 쓰지 않는다.
영향 SEVERE/CAUTION/REFERENCE와 작가 판단은 별개이며 NO_CONFLICT에는 영향 등급을 붙이지 않는다.

`InspectionResult`에는 status, questions, decisions, receipts, coverage, failure_code와 evidence_bundles, `failure_cause`(분류 코드 아래의 구체 고정 코드),
`failure_stage`(`PLANNER:TSEG1`, `JUDGE:Q3` 같은 단계), `normalizations`(정규화·검색·캐시 횟수), `question_failures`, `traces`가 있다.
원문·예외 메시지·ID는 넣지 않는다. 미완료는 정상 0건으로 집계하지 않는다. 입력/출처 변경·파기를 감지하면 이전의 게시 가능한 질문/판정/근거를 반환하지 않는다.
최종 VALIDATE 재확인은 없다: 원고·설정이 바뀌면 서버가 report를 무효로 바꾸고 Worker의 모든 요청을 거절한다.

## 모델·예산·오탈자 공통 계약

모델과 추론 강도는 코드에 고정하지 않고 Job configuration v2의 역할별 설정이 정한다(`claim_roles`). 운영 권장값은 PLANNER `gpt-6.1-terra` medium이고
JUDGE·TYPO는 31화로 Terra와 `gpt-6-luna`를 비교한 뒤 정한다(판정 medium, 오탈자 low). 출력 상한도 역할별이다(계획 5만, 판정 2만, 오탈자 1.2만).
frozen `LlmCallOptions`를 count/생성 양쪽에 동일하게 전달하고 공유 client 기본 속성을 바꾸지 않는다. 기존 추출/챗봇의 모델·추론 기본값은 유지한다.

`InvocationBudget`의 model_calls/lookups/seconds/input_tokens/segment_chars/context_chars/search_limit는 모두 호출자가 정한다(claim 시도 단위).
운영 예산(실행 한 번): 모델 호출 200, 조회 300, 60분, 호출 한 번 입력 8만, top-k 10. 역할 cap(계획 10회/80만, 판정 200회/1,000만, 검색어 embedding 250회/40만)은
서버가 실행 단위로 센다([실행 계약](validation-execution-v1.md)). 필수 문맥 초과·호출 실패·출력 잘림·예산 종료는 미완료다.
실제 검색/embedding async 경계도 호출 전에 lookup을 소비하고 남은 seconds로 wait_for한다. 시간 초과는 BUDGET_EXHAUSTED다.
코루틴의 대기 취소가 asyncio.to_thread 안에서 이미 실행 중인 동기 SQL/DB 작업을 강제로 종료한다는 뜻은 아니다. 호출자는 읽기 연결의 DB statement/connection/pool timeout을 함께 설정해야 한다.
`TypoCheckInput/TypoSuggestion/TypoCheckResult`, Span, split_segments와 coverage/failure/options는 GH309가 재사용한다. `check_typos(..., role=RoleRuntime)`가
TYPO 모델·추론 강도·출력 상한을 받는다. 오탈자 실행·작가 수정 판단·원문 자동 편집은 여기서 구현하지 않는다.

로컬 실험 harness(`scripts/validation_experiment/`)는 같은 계획·판정·오탈자 코드를 실행 Job 없이(`checkpoint=None`, `experiment=True`) 읽기 전용 export로 만든
fixture와 loopback 일회용 DB(`validation_experiment_*`)에서 돌린다. 유료 단계는 `--confirm-paid`가 필요하고 출력은 저장소 밖에 둔다.

## 재현

AI: 기존 venv의 Python을 명시하고 이 checkout에서 PYTHONPATH=.로 실행한다.

```text
<python> -m pytest tests/test_validation_call_options.py tests/test_validation_contracts.py
  tests/test_validation_segmentation.py tests/test_validation_judging.py
  tests/test_validation_data_client.py tests/test_setting_validation_service.py -q
<python> -m pytest tests/test_validation_postgres.py -q
```

실제 PG는 새 loopback gh306_test와 GH306_TEST_DATABASE_URL/GH306_JAVA_CHECKOUT만 허용한다.
Java는 GH306_POSTGRES_JDBC_URL=jdbc:postgresql://127.0.0.1:<port>/gh306_test를 명시해 ValidationDataPostgresIntegrationTest를 실행한다.
GH306_AI_CHECKOUT/GH306_PYTHON을 함께 주면 실제 Spring HTTP→Python adapter를 검증한다.
기본 관련 전체 검증은 AI pytest, Backend ./gradlew test bootJar다. 선택 환경 미지정 skip을 통과로 세지 않는다.
테스트는 합성 원고·모델 출력/HTTP provider 대역·원문 저장소 대역과 실제 DB/HTTP를 구분한다. 실제 Terra recall이나 운영 배포 검증으로 표현하지 않는다.
