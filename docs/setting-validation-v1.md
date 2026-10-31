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
2. Terra medium이 질문을 계획한다. 구간 문맥에 이름·별칭이 글자 그대로 나온 대상(기계 매칭)과 Terra high SUBJECT_RESOLUTION이 첫 질문 없이 해소한 비문자 지칭(대명사·호칭·직함·별명·부분 이름)의 대상을 합쳐 관련 대상으로 삼는다(GH349).
   Terra high COVERAGE는 그 대상들의 고정 항목 전체의 제약·이력·조건에서 별도 질문을 만든다. 전체 카탈로그를 페이지별로 선정하지 않는다.
3. 날짜/수량/요일/반복 명칭의 결정적 단서를 독립 수집한다. 보완은 선택 규칙·구간·단서마다 CHECKED/NOT_APPLICABLE/UNPROCESSED를 구분한다. 이 기록이 의미상 누락 0을 보증하지 않는다.
4. 같은 제공 대상·속성/규칙·서사 시점·조건·서술 방식의 질문만 병합한다. 모든 위치·원 질문 참조·조회 의무를 보존하며 ID 미해결 질문은 보수적으로 별도 유지한다.
5. 질문별 실제 고정 DB/target 조회 영수증과 추가 조회를 묶는다. Terra medium은 FOLLOW_UP 또는 DECIDED를 반환한다. 새 단서가 영향을 주는 기존 질문을 재개하고 revision/input_digest를 갱신한다.
6. 코드가 실제 제공 참조·정확한 모든 target 위치·성공 receipt·필수 읽기 처리·후속 단서 미처리 없음·실행 입력을 검사한다. 모델의 true 네 개를 그대로 완료 조건으로 쓰지 않는다.

GH331 모델 입력·출력 계약(실제 Terra 관측 반영):

- PLANNER/COVERAGE 입력은 `target_source_ref: "T1"`과 대상 식별용 `subject_catalogue`를 준다. COVERAGE는 선택한 `settings`와 그 entry_ref 목록 `rule_refs`를 따로 받고
  rules에는 rule_refs만 기록한다. GH349부터 `subject_catalogue`는 항목별 행이 아니라 주체별 한 행(`subject_ref`, `domain`, 첫 항목의 `name`,
  항목 전체 `aliases`의 순서 보존·중복 없는 합집합)이며 PLANNER·SUBJECT_RESOLUTION·COVERAGE가 같은 목록을 받는다(식별용, 질문 subject_refs 검증은 전체 주체 기준).
- 역할별 strict schema(`validation_<role>_v2`)는 span `source_ref=T1`과 연산별 lookup intent anyOf(BASELINE=B, TARGET_CONTEXT=T1/null, DIRECT=R 필수,
  KEYWORD/VECTOR=query 필수, NEIGHBORS=E만)를 쓴다. COVERAGE·BOOK_COVERAGE는 선택 rule·CL 참조를 enum으로 열거한다(항목당 100·schema당 500개까지, 넘으면 문자열).
  SUBJECT_RESOLUTION(GH349)은 `subject_ref`를 보고서의 S 참조 enum 또는 null로 제한하고(100개 초과는 S pattern, 주체가 없으면 null만) 보고서당 한 schema로 고정한다.
  PLANNER·JUDGE는 같은 system prompt로 여러 번 호출되므로 provider prompt cache가 유지되도록 보고서 안에서 schema를 고정한다. 참조는 종류별 pattern(B/R/E/L/Q/S/TSEG/CL)으로만 제한하고
  실제 ref 형식이 다르면 일반 문자열로 둔다. 정확한 소속은 검증기가 판단한다.
- frozen RAG 검색이 주입되지 않았고 회차 출처가 있으면 VECTOR는 항상 `VECTOR_NOT_READY`로 끝나므로 schema가 KEYWORD만 제안하고, 남은 VECTOR 의무는 저장 전에 거절한다.
  검색이 주입된 실행과 설정집만 있는 실행은 기존 VECTOR 경로(색인 readiness 검사·책 raw fallback)를 그대로 쓴다.
  GH344: 검색이 주입돼도 회차 출처가 있으면 Job 시작 때 고정 범위의 readiness를 한 번 읽어, 모든 고정 회차 출처의 vector가 준비됐고(`vector_chunks>0`,
  `vector_ready_sources=source_count=회차 출처 수`) query embedding client가 있을 때만 VECTOR를 제안한다. 읽기는 `min(남은 seconds, 5초)` 안에 끝나야 하며,
  실패·초과하면 경고(예외 종류만)를 남기고 그 Job은 frozen RAG 없이(키워드만, 원문 직접 읽기) 계속한다. Job을 실패시키지 않는다.
  `normalizations.VECTOR_OFFERED`(1/0)와 INFO `vector search offered=...`로 남긴다. 조회 직전의 readiness 검사(`VECTOR_NOT_READY`·`VECTOR_INDEX_PARTIAL`)는 그대로 둔다.
  같은 Job의 이전 시도가 VECTOR 계획·의무·receipt를 이미 저장했으면 재개 때 readiness와 무관하게 VECTOR를 유지해(`resumed=True`) 성공한 조회의 JUDGE-only 재개를 막지 않는다.
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

GH341: SUSPECTED_CONFLICT의 변화·예외 검색 의무는 JUDGE가 답한 뒤에야 드러나므로 Worker가 결정적으로 보완한다(계약 자체는 그대로다).

- DECIDED SUSPECTED_CONFLICT인데 그 질문에 성공한 `CHANGES_EXCEPTIONS` KEYWORD/VECTOR receipt가 없으면, 판정 검증 전에 KEYWORD 검색 하나를
  조회 의무로 넣는다. ref `Qn-CHANGES_EXCEPTIONS`, 고정 purpose `Worker가 충돌 의심 판정 전에 요구하는 변화·예외 검색`, query는 중복 없는 비어 있지 않은
  `subject_mentions` 뒤에 `property_or_rule`을 공백으로 이어 1000자 이내로 자른 값이다. FOLLOW_UP과 같이 검증·revision(`NEW_CLUE`)·재대기한 뒤
  검색 receipt와 근거를 받은 JUDGE를 다시 호출한다. frozen RAG 유무와 관계없이 항상 KEYWORD다.
- 질문당 한 번만 넣는다. 같은 purpose의 검색이 대기 중이거나(재개 포함) 이미 실행됐으면 다시 넣지 않는다. 검색 자체가 실패하면 기존처럼 그 조회의
  실패 코드로 미완료이며 같은 입력의 재시도는 저장된 의무를 다시 실행할 뿐 새로 넣지 않는다. query를 만들 수 없으면 `INVALID_DECISION_CHANGE_SEARCH_MISSING`이 그대로다.
- 다시 받은 SUSPECTED_CONFLICT의 `completion.changes_exceptions`가 성공 검색 receipt를 인용하지 않았는데 질문에 그런 receipt가 있으면, Worker가 그 receipt 전체와
  그 근거 중 알려진 참조(없으면 `T1`)를 덧붙인다. 모델이 인용한 receipt·근거는 지우지 않고 그대로 검증하며 없는 참조를 만들지 않는다.
- 횟수는 `normalizations`의 `CHANGE_SEARCH_INSERTED`(넣은 검색), `CHANGE_SEARCH_RECEIPT_FILLED`(인용 보완)로 남는다. 판정·사유·영향 등급·prompt·schema·예산은 바꾸지 않는다.

GH349: 관련 설정 선정을 전체 카탈로그 페이지 선별(RELATED_SELECTION:TSEGn:PAGEk)에서 주체 매칭 + 구간당 SUBJECT_RESOLUTION으로 바꾼다(grid v2, [실행 계약](validation-execution-v1.md)).

- 기계 매칭: 항목의 2 code point 이상인 `name` 또는 `aliases` 문자열이 구간 문맥(`text[contextStart:contextEnd]`, code point)에 정확한 부분 문자열로 나오면
  그 항목의 주체가 구간에 매칭된다. 정규화하지 않고, S 번호 순·중복 없음이며, name이 null이고 aliases가 비면 매칭되지 않는다(1글자 규칙은 아래 GH349 후속).
  실행 Job은 서버 grid의 `segmentSubjects`만 쓴다(첫 의미 단계 게시 뒤 checkpoint에서 다시 읽음). grid가 정수 2·3이 아니거나 `segmentSubjects`가 없으면
  `GRID_VERSION_UNSUPPORTED`, 구간·주체 참조가 맞지 않으면 `INVALID_GRID_CONTRACT`다. Job 없는 호출형만 같은 규칙을 로컬에서 계산한다.
- SUBJECT_RESOLUTION은 구간마다 PLANNER 다음·COVERAGE 전에 한 번이다. 모델 호출 역할은 기존 `RELATED_SELECTION`(Terra high, 같은 역할 예산·원장)이고
  단계 이름만 새롭다. 입력은 구간 공통 입력 + `subject_catalogue` + `mechanical_subject_refs`, 출력·저장은 `{"mentions":[{"mention","subject_ref","reason"}]}`다.
  mention·reason은 공백이 아니고 mention은 중복되지 않으며 subject_ref는 null 또는 카탈로그 S 참조다. 위반은 `SUBJECT_RESOLUTION_INVALID`
  (원인 `SUBJECT_RESOLUTION_BLANK`·`_DUPLICATE_MENTION`·`_UNKNOWN_SUBJECT`)이고 저장하지 않는다. 이미 매칭된 주체를 가리키는 mention은 허용하고 무시한다.
- 해소할 주체가 남지 않으면(카탈로그 주체 0개이거나 모든 주체가 이미 기계 매칭) 모델 출력이 결과를 바꿀 수 없으므로 호출 없이 `{"mentions": []}`를 저장한다.
  이전에도 빈 카탈로그는 선정 호출이 0이었다. 저장된 단계는 재개 때 다시 호출하지 않는다.
- 관련 주체 U = 기계 매칭 ∪ null이 아닌 subject_ref. COVERAGE의 `settings`·`rule_refs`는 U에 속한 모든 항목(카탈로그 순서)이며 비어 있을 수 있다.
  COVERAGE의 나머지 계약(구간·질문·clue_checks·span 범위)은 그대로다.
- 모델 호출은 카탈로그 크기와 무관하게 구간당 최대 1회다(예: 120항목·페이지 50이면 구간당 3→1). 고정 항목 읽기 페이지(`catalogue_page_size`)는 그대로다.
  coverage에는 `{"role": "SUBJECT_RESOLUTION", "segment_ref", "mechanical_subject_refs", "resolved_subject_refs", "mentions"}`(mentions는 개수)를 남긴다.
- 한계: 이름이 다른 단어의 일부로 나와도 매칭된다(정규화 없음, 과포함 방향). 이름이 같은 다른 주체도 함께 매칭된다. 지칭 해소의 실제 모델 품질은 아직 측정하지 않았다.

GH349 후속(2026-10-31 첫 운영 실행): 30회차 작품에서 COVERAGE:TSEG1이 호출 전에 `REQUIRED_CONTEXT_TOO_LARGE`(입력 > `setting.input-tokens` 80,000)로 멈췄다.
호출 수가 아니라 항목 크기가 원인이었다(관련 항목 179개 raw JSON 795 KB, 가장 큰 인물 항목 362 KB 중 slots 149 KB·historyFacts 209 KB). 세 가지를 바꾼다.

- 모델 입력 투영: COVERAGE와 JUDGE의 `settings`는 `app/validation/projection.py`의 `compact_setting_row`로 만든 압축 행이다.
  행은 `entry_ref`, `subject_ref`, `domain`, `name`, `aliases`, `state`(+행에 있으면 `author_correction` 그대로)다. `state`에는 스칼라 값, 스칼라 목록(예: aliases),
  `currentNarrativeTime`·`narrativeTime`·`absences`·`protectedSlots`·`protectedPaths`(안의 근거 키 제거), 세계관 값 `propertiesJson`(작가 설정명이라 그대로)과
  `slots`(슬롯마다 `factKey`·`factType`·`factValue`, 스칼라인 `temporalScope`·`effectiveFromEpisodeNo`만)만 남긴다.
  `historyFacts`, `supportingSources`, `supportingSource`, `provenance`, `provenanceByPath`, `identityEvidence`, `removedSnapshotEntries`, `valueJson`, `evidenceSpans`,
  `comparison`, `explicitAuthorCorrections`, `snapshotVersion`, `currentSupportingEvidenceVersion`, `version`과 그 밖의 중첩 목록·객체는 뺀다.
  모델이 보는 것과 달리 고정 항목 읽기(`read_entries_page`), Worker 검증, receipt는 전체 투영 그대로다. 게시·checkpoint `evidence_bundle`의 `settings`는
  같은 항목의 ref 행만 담는다(아래 bundle ref 행, GH349 후속 2). 질문의 조회 의무나 JUDGE FOLLOW_UP의
  BASELINE이 `evidence_refs`로 요청한 항목은 그 질문의 다음 JUDGE 입력에 전체 행으로 준다. system prompt 한 줄이 이 압축과 BASELINE 요청 방법을 알린다.
- 1글자 이름: 2 code point 미만의 name·alias는 기계 매칭에서 뺀다(1글자 이름이 거의 모든 문맥에 걸려 구간당 16~22주체가 COVERAGE에 들어갔다).
  서버 SETTING grid는 같은 규칙으로 `gridVersion: 3`(모양은 v2와 같음)이며 Worker는 2·3을 모두 받는다. `subject_catalogue`에는 그대로 남아 SUBJECT_RESOLUTION이 문맥으로 해소할 수 있다.
- 입력 상한 안전장치: 관련 항목이 `MAX_COVERAGE_SETTINGS`(400)를 넘으면 카탈로그 순서의 앞 400개만 COVERAGE 모델에 주고(`rule_refs`·schema enum도 400개),
  나머지는 모델 응답 뒤 Worker가 `NOT_APPLICABLE` + `입력 상한으로 모델 점검에서 제외됨`으로 rules에 더한다. 그래서 저장 rules는 여전히 관련 항목 전체이며
  Worker와 Backend의 rule 집합 검사를 그대로 통과한다. 재개한 batch에는 이미 있으므로 다시 더하지 않는다. WARNING `coverage settings truncated total=… kept=… segment=…`와
  `normalizations.COVERAGE_SETTINGS_TRUNCATED`(구간 합계)를 남긴다. 주 해결책은 압축 투영이며 제외된 항목은 이 구간의 COVERAGE가 읽지 않았다는 뜻이다. JUDGE에는 상한이 없고 질문 주체의 항목 전체를 압축 행으로 받는다.
- bundle ref 행(GH349 후속 2): 게시 명령은 전체 131,072 byte 상한인데 Backend가 JUDGE bundle `settings`를 질문 주체 항목 전체의 정본 행과 비교해 362 KB 인물 하나로도
  그 주체의 판정을 게시할 수 없었다. 이제 bundle `settings` 행은 `{"entry_ref", "subject_ref"}`(`bundle_settings`, 이전과 같은 관련 항목 집합·순서)이고 `input_digest`도
  이 bundle로 계산한다. Backend는 ref 집합(질문 주체의 항목 ∪ BASELINE 조회 ref), 정본 항목 여부, subject_ref 일치, 중복만 검사한다. `InspectionResult.evidence_bundles`도 같은 모양이다.
  이 변경 전에 전체 행으로 결정된 check를 재개하면 entry_ref/subject_ref만 읽어 같은 모양으로 돌려주고 다시 JUDGE하거나 게시하지 않는다.

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
RELATED_SELECTION은 독립 누락 보완 역할의 호출이며 다섯 번째 별도 모델 선택이 아니다. GH349부터 이 역할은 SUBJECT_RESOLUTION 단계를 호출한다.
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
