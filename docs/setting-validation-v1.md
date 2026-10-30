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

공통 요청: `expectedBaselineHash`, `expectedTargetHash`, `operation`, `offset >= 0`, `1 <= limit <= 100`.
`INPUT`은 고정 target·허용 sources·entryCount, `ENTRIES`는 저장된 baseline_entries의 페이지를 반환한다.
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

`FrozenRetrievalRepository`는 DB report와 validation_baseline_sources를 모든 direct/keyword/vector/neighbors의 공통 SQL 조건에 결합한다.
회원/작품/수명·target·정확한 허용 출처 hash/key/version·대상 이전 회차 필터를 순위/LIMIT/인접 확장 전에 적용한다.
`FrozenRetrievalSearchService`는 호출자가 명시적으로 주입한 읽기 session factory만 사용하며 기본 DB/embedding provider를 만들지 않는다.
검색 결과도 검수 reader의 실제 원문 slice와 대조한다. 벡터 호출에는 같은 index spec의 계량 embedding client를 주입한다.

`ValidationCorpus`는 색인이 완전한 회차의 DB 검색과 미색인 회차/설정집의 실제 고정 원문 분할·키워드/직접/인접 읽기를 결합한다.
DB Fact에 연결되지 않은 과거 자료도 검색한다. keyword의 색인/비색인 결과를 함께 순위화한 뒤 제한한다.
설정집은 fake 회차/chunk를 만들지 않고 실제 revision/referenceTimeKind/referenceEpisodeNo를 유지한다.
DB 항목/첫 질문이 0개여도 고정 설정집 전문을 실제로 읽고 구간별 high 독립 보완에 raw_rules로 제공한다.
BOOK_SOURCE의 예정/완료 점검 수와 BOOK_COVERAGE의 구간/규칙 처리 기록을 남긴다. 읽기 실패·누락 규칙·잘림·예산 부족이면 완료하지 않는다.
각 raw rule은 실제 E 근거를 연결하며 보완이 만든 질문은 그 근거를 판정 입력에 유지한다. 원문을 설정 후보로 저장하지 않는다.
과거 전문을 LLM에 풀스캔하지 않는다. 코드의 원문 검색이 실패하거나 필요한 budget이 끝나면 미완료다.
벡터가 필요하지 않으면 실행하지 않으며 요청한 필수 벡터를 실행할 수 없으면 VECTOR_NOT_READY다.
필수 벡터의 **허용 회차**에 미준비 범위가 있으면 실제 readiness/limitations를 실패 receipt에 보존하고 VECTOR_INDEX_PARTIAL로 미완료 처리한다.
벡터가 불필요한 직접·keyword 완료는 벡터 미준비만으로 실패시키지 않는다. 설정집의 새 vector index는 필수가 아니며 실제 raw 읽기/keyword fallback과 실행한 방법을 명시한다.

## 질문·독립 보완·판정

`SettingValidationService(data_client, model_client, budget, search=None, embedding_client=None).inspect()`가 진입점이다.
`model_client`는 create_text_response와 exact count_input_tokens를 제공해야 한다.
검수 과금/Worker 원장 연결은 GH310의 책임이다. 기존 추출용 MeteredTextGenerationClient를 재사용하기 위해 가짜 AnalysisJob/lease를 생성하지 않는다.

1. 실제 target 전문과 고정 항목 페이지를 준비한다. 순수 분할기의 모든 핵심 구간이 전문을 덮고 앞뒤 문맥을 별도로 유지한다.
2. Terra medium이 질문을 계획한다. Terra high는 첫 질문 없이 원고와 전체 카탈로그 페이지에서 대상을 독립 선정하고 선택한 제약·이력·조건에서 별도 질문을 만든다.
3. 날짜/수량/요일/반복 명칭의 결정적 단서를 독립 수집한다. 보완은 선택 규칙·구간·단서마다 CHECKED/NOT_APPLICABLE/UNPROCESSED를 구분한다. 이 기록이 의미상 누락 0을 보증하지 않는다.
4. 같은 제공 대상·속성/규칙·서사 시점·조건·서술 방식의 질문만 병합한다. 모든 위치·원 질문 참조·조회 의무를 보존하며 ID 미해결 질문은 보수적으로 별도 유지한다.
5. 질문별 실제 고정 DB/target 조회 영수증과 추가 조회를 묶는다. Terra medium은 FOLLOW_UP 또는 DECIDED를 반환한다. 새 단서가 영향을 주는 기존 질문을 재개하고 revision/input_digest를 갱신한다.
6. 코드가 실제 제공 참조·정확한 모든 target 위치·성공 receipt·필수 읽기 처리·후속 단서 미처리 없음·실행 입력을 검사한다. 모델의 true 네 개를 그대로 완료 조건으로 쓰지 않는다.

GH331 모델 입력·출력 계약(실제 Terra 관측 반영):

- PLANNER/COVERAGE 입력은 `target_source_ref: "T1"`과 대상 식별용 `subject_catalogue`를 준다. COVERAGE는 선택한 `settings`와 그 entry_ref 목록 `rule_refs`를 따로 받고
  rules에는 rule_refs만 기록한다. RELATED_SELECTION은 기존처럼 page의 `catalogue`만 받는다.
- 역할별 strict schema(`validation_<role>_v2`)는 span `source_ref=T1`과 연산별 lookup intent anyOf(BASELINE=B, TARGET_CONTEXT=T1/null, DIRECT=R 필수,
  KEYWORD/VECTOR=query 필수, NEIGHBORS=E만)를 쓴다. COVERAGE·BOOK_COVERAGE·RELATED_SELECTION은 선택 rule·CL·page 참조를 enum으로 열거한다(항목당 100·schema당 500개까지, 넘으면 문자열).
  PLANNER·JUDGE는 같은 system prompt로 여러 번 호출되므로 provider prompt cache가 유지되도록 보고서 안에서 schema를 고정한다. 참조는 종류별 pattern(B/R/E/L/Q/S/TSEG/CL)으로만 제한하고
  실제 ref 형식이 다르면 일반 문자열로 둔다. 정확한 소속은 검증기가 판단한다.
- frozen RAG 검색이 주입되지 않았고 회차 출처가 있으면 VECTOR는 항상 `VECTOR_NOT_READY`로 끝나므로 schema가 KEYWORD만 제안하고, 남은 VECTOR 의무는 저장 전에 거절한다.
  검색이 주입된 실행과 설정집만 있는 실행은 기존 VECTOR 경로(색인 readiness 검사·책 raw fallback)를 그대로 쓴다.
- strict mode는 root 조건부 필드를 표현할 수 없으므로 판정 조건은 prompt와 검증기가 맡는다. FOLLOW_UP의 outcome/basis/impact/impact_reason/completion은 사용하지 않으므로 무시(null)하고,
  NO_CONFLICT의 impact/impact_reason도 finding이 아니어서 null로 둔다. 나머지 판정의 impact 누락 등은 그대로 거절한다.
- `completion.required_reads.receipt_refs`는 Worker가 질문의 실제 성공 receipt 전체로 확정한다. 모델이 인용한 receipt/근거가 실제로 없으면 거절하고, 빠진 receipt만 채운다.
  다른 세 completion 항목은 근거·receipt를 하나 이상 인용해야 한다.
- DECIDED의 `target_spans`도 질문 span 자체다(Backend가 같은 집합을 요구). 모델이 되풀이한 span은 위치 재계산 뒤 원고의 정확한 인용인지만 확인하고 질문 span으로 확정한다.
  원고에 없는 인용은 `INVALID_EXACT_SPAN`으로 거절한다.
- PLANNER/COVERAGE/BOOK_COVERAGE의 조회 의무와 JUDGE FOLLOW_UP은 저장·revision 전에 읽을 수 있는 참조인지 검사한다(`UNKNOWN_BASELINE_REFERENCE`, `UNKNOWN_SOURCE_REFERENCE`,
  `UNKNOWN_EVIDENCE_REFERENCE`, `INVALID_TARGET_CONTEXT_REQUEST`, `INVALID_SEARCH_REQUEST`). 저장되지 않으므로 같은 입력 재시도는 모델을 다시 호출한다.

결과는 CONFLICT / SUSPECTED_CONFLICT / NEEDS_CONTEXT / NO_CONFLICT이며 실패는 INCOMPLETE로 별도다.
SUSPECTED_CONFLICT/CHANGE_NOT_FOUND는 실제 성공한 변화/예외 keyword/vector 검색 receipt가 있어야 한다.
DB/직접 입력과 이미 확보한 근거가 충분하면 추가 검색을 생략할 수 있으나 대상·시점·조건·변화/예외 설명과 실제 자료 참조가 필요하다.
검색 0건 자체로 직접 모순이나 예외 부재를 증명하지 않는다. 같은 신규 회차만의 모순은 판정하지 않는다.

DB↔원문 불일치는 두 참조를 유지하고 명시 정정의 정확한 대상/항목/시점/조건 범위를 검증한다.
기존 데이터에는 그 scope metadata가 없을 수 있다. 그런 경우 보호/확정 flag를 정정으로 승격하지 않고 NEEDS_CONTEXT로 둔다.
명시 scope가 있는 입력만 해당 범위의 정정을 우선할 수 있다. 이 검사기가 정정 metadata나 원문/DB를 쓰지 않는다.
영향 SEVERE/CAUTION/REFERENCE와 작가 판단은 별개이며 NO_CONFLICT에는 영향 등급을 붙이지 않는다.

`InspectionResult`에는 status, questions, decisions, receipts, coverage, failure_code와 evidence_bundles가 있다.
GH331부터 `failure_cause`(분류 코드 아래의 구체 고정 코드, 예: `INVALID_INSPECTION_CONTRACT` 아래 `INVALID_EXACT_SPAN`, `INVALID_DECISION` 아래 `INVALID_DECISION_REQUIRED_READS`),
`failure_stage`(`PLANNER:TSEG1`, `JUDGE:Q3` 같은 단계), `normalizations`(정규화 종류별 횟수)도 함께 반환하고 같은 값을 로그에 남긴다. 원문·예외 메시지·ID는 넣지 않는다.
미완료는 정상 0건으로 집계하지 않는다. 입력/출처 변경·파기를 감지하면 이전의 게시 가능한 질문/판정/근거를 반환하지 않는다.
일반 읽기/모델 실패는 완료된 독립 범위를 보존할 수 있지만 전체 status는 INCOMPLETE다. 영속 재시도 정책은 GH310에서 연결한다.

## 모델·예산·오탈자 공통 계약

모든 역할 모델은 `gpt-6.1-terra`다. PLANNER medium, RELATED_SELECTION/COVERAGE high, JUDGE medium, TYPO low다.
RELATED_SELECTION은 독립 누락 보완 역할의 호출이며 다섯 번째 별도 모델 선택이 아니다.
frozen `LlmCallOptions`를 count/생성 양쪽에 동일하게 전달하고 공유 client 기본 속성을 바꾸지 않는다.
기존 추출/챗봇의 모델·추론 기본값은 유지한다. 실제 Terra provider 품질 호출은 이번 검증에서 실행하지 않았다.

`InvocationBudget`의 model_calls/lookups/seconds/input_tokens/output_tokens/segment_chars/context_chars/catalogue_page_size/search_limit는 모두 호출자가 정한다.
합의된 운영 횟수·시간·비용 수치나 무한 loop를 기본값으로 넣지 않는다. 필수 문맥 초과·호출 실패·출력 잘림·예산 종료는 미완료다.
실제 검색/embedding async 경계도 호출 전에 lookup을 소비하고 남은 seconds로 wait_for한다. 빈 결과 반복도 예산을 소비하며 시간 초과는 BUDGET_EXHAUSTED다.
코루틴의 대기 취소가 asyncio.to_thread 안에서 이미 실행 중인 동기 SQL/DB 작업을 강제로 종료한다는 뜻은 아니다. 호출자는 읽기 연결의 DB statement/connection/pool timeout을 함께 설정해야 한다. 검사 서비스는 기한에 미완료를 반환하며 결과/현재값을 쓰지 않는다.
`TypoCheckInput/TypoSuggestion/TypoCheckResult`, Span, split_segments와 coverage/failure/options는 GH309가 재사용한다.
오탈자 실행·작가 수정 판단·원문 자동 편집은 여기서 구현하지 않는다.

## 재현

AI: 기존 venv의 Python을 명시하고 이 checkout에서 PYTHONPATH=.로 실행한다.

```text
<python> -m pytest tests/test_validation_call_options.py tests/test_validation_contracts.py
  tests/test_validation_data_client.py tests/test_setting_validation_service.py -q
<python> -m pytest tests/test_validation_postgres.py -q
```

실제 PG는 새 loopback gh306_test와 GH306_TEST_DATABASE_URL/GH306_JAVA_CHECKOUT만 허용한다.
Java는 GH306_POSTGRES_JDBC_URL=jdbc:postgresql://127.0.0.1:<port>/gh306_test를 명시해 ValidationDataPostgresIntegrationTest를 실행한다.
GH306_AI_CHECKOUT/GH306_PYTHON을 함께 주면 실제 Spring HTTP→Python adapter를 검증한다.
기본 관련 전체 검증은 AI pytest, Backend ./gradlew test bootJar다. 선택 환경 미지정 skip을 통과로 세지 않는다.
테스트는 합성 원고·모델 출력/HTTP provider 대역·원문 저장소 대역과 실제 DB/HTTP를 구분한다. 실제 Terra recall이나 운영 배포 검증으로 표현하지 않는다.
