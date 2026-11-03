# 검수 실행·재검수 계약 v1 — GH310

이 문서는 변경 입력의 명시적 재검수·실제 설정 정정과 Java 전용 검수 Job/lease·결과 저장 계약을 정의한다.
Python 전용 EpisodeValidationWorker는 이 계약으로 실제 검사 실행·의미 상태 저장·같은 입력 재개를 연결한다. 작가 판단·검토 종료·추출 허가는 GH312,
화면과 다음 회차 등록 제한은 GH314다. 이 단위에서 추출 Job·후보·설정 자동 확정·자동 철회는 생성하지 않는다.
합성 요청 예시는 `docs/contracts/validation-execution-v1.json`이다.

## 이력과 버전

`baselineId = reportId`를 유지한다. report마다 고정 원고와 새 기준을 캡처하며 과거 판단을 복사하지 않는다.
원고만 바뀐 경우에도 이 구현은 접근 가능한 자료에서 새 기준을 캡처한다. 이전 복사본을 복구하지 않는다.

| 필드 | 의미 |
| --- | --- |
| `current` | 회차당 하나인 현재 report 포인터. INVALIDATED여도 새 재검수 전까지 현재 포인터다 |
| `revision` | 회차 안의 새 report 접수 순번, 최초 1 |
| `reportVersion` | 현재 report 상태의 CAS. 조회나 flush만으로 증가하지 않는다 |
| `baselineRevision` | 새로 캡처한 기준 순번, 최초 1. 현재 구현은 report마다 새 기준이므로 두 순번이 함께 증가한다 |
| `targetRevision` | report가 고정한 `Episode.contentRevision` |
| `baselineHash` | 현재 읽을 수 있는 저장 seed hash. 무효화하면 null |
| `baselineFingerprint` | 파기 뒤에도 비교할 수 있는 해시 식별값. 본문 접근·추출 허가가 아니다 |
| `previousReportId` | 바로 앞 report ID. 과거 응답은 요약만 제공한다 |
| `mode` | 서버가 실제 확정/적용 이력으로 정한 BEFORE_CONFIRMATION 또는 DIAGNOSTIC_ONLY |

Work 잠금이 report 쓰기의 직렬화 경계다. 최초 접수·새 재검수는 Work → report/원고/기준 순서,
정정은 Work → report → 설정 대상 순서를 따른다. PostgreSQL partial unique index는 회차당 현재 포인터 하나를,
`(episode_id, revision)` unique index는 같은 이력 순번의 중복을 막는다. 이전 포인터를 flush한 뒤 새 report를 저장한다.

report version은 seed 캡처·정규화 완료·보류·최초 무효화·현재 포인터 퇴역에서 증가한다.
반복 파기와 조회는 증가시키지 않는다. 후속 실행/결과 소유자도 Work 잠금 아래 의미 있는 변경 후
`ValidationReport.advanceVersion()`을 호출해야 한다. JPA `@Version` 대신 명시적인 version을 사용한다.
JSON dirty checking의 flush가 내용 변화 없이 CAS를 바꾸는 현상을 실제 통합 테스트에서 확인했기 때문이다.

원고 revision은 원문 수정·저장 위치 변경·파일 교체·파기마다 증가한다. 제목만 수정하면 유지한다.
동일 hash/key/version으로 돌아온 A→B→A도 이전 report 입력을 되살리지 않는다.
회차 상세 `contentRevision`과 원고 fingerprint를 함께 읽고 재검수에 전달한다.

## 공개 접수 API

기존 `POST /api/v1/works/{workId}/episode-validations`는 신규 단일 append 업로드의 최초 접수다.
동일 START 키는 과거 접수 응답도 요약으로 복구한다. 새 키로 최초 접수를 재검수로 대체할 수 없다.
기존 `GET /{reportId}`는 현재/과거 요약을, `GET /current?episodeId=...`는 현재 report와 CAS version을 반환한다.

`POST /{reportId}/rechecks`의 필수 입력은 `idempotencyKey`, `expectedReportVersion`,
현재 `expectedContentHash/expectedContentS3Key/expectedContentRevision`이다. S3 version은 실제 nullable 값을 보낸다.
`expectedBaselineHash` 또는 `correctionReceiptId`를 제공한다. 둘 다 보내면 서로 같은 기준이어야 한다.
키는 START/RECHECK가 공유하는 작품별 namespace이고 같은 키의 다른 operation/입력은 409다.
같은 입력의 실패 재시도는 새 report를 만들지 않고 `VALIDATION_RECHECK_INPUT_UNCHANGED`로 별도 retry 경계를 안내한다.
오래된 report·version은 `VALIDATION_REPORT_VERSION_CONFLICT`, 원고/기준 불일치는
`VALIDATION_SOURCE_VERSION_CONFLICT`다. 새 접수는 최신 활성 회차에만 허용한다.

V91에서 이미 파기해 fingerprint가 없거나 최초 캡처 실패로 seed/hash가 없는 현재 report만
`expectedBaselineHash=null`을 받을 수 있다. 소유권·현재 reportVersion·현재 원고 fingerprint/revision·최신 회차를
검증해 새 기준/report를 준비한다. 기존 기준·판단을 복원하는 동일 입력 재시도가 아니다.
서버 fingerprint가 있으면 null 또는 다른 hash를 거절한다. 이전 해시를 추측하거나 backfill하지 않는다.

## 명시적 기준 정정

`POST /{reportId}/corrections`는 `ValidationCorrectionRequest`로 실제 설정 편집과 receipt를 한 트랜잭션에 묶는다.
필수 공통 필드는 reportVersion, baselineId/revision/hash, `entryRef`와 `expectedEntryHash`, 멱등 키다.
키는 정정 endpoint의 별도 작품별 namespace이며 동일 키·다른 report/입력은 409다.
`ENTRIES` 응답의 entryHash는 저장 항목 자체의 hash이며 전송용 entryRef/entryHash를 다시 hash하지 않는다.

`character`와 `worldSetting` 중 정확히 하나만 받는다.

- 캐릭터: targetId, expectedSnapshotVersion, factType/factKey, 기존 전체 CharacterUpdateRequest를 받는다.
  기준의 현재 전체 slot과 실제 authoritative snapshot·현재 version이 같아야 한다.
  기존 CharacterService의 동일 변환/스키마/수동 Fact 경계를 사용하되 지정한 현재 slot 하나 외의
  추가·삭제·값 변경, 이름/역할/첫 등장 변경은 저장 전에 거절한다. 일반 Character PATCH의 기존 계약은 유지한다.
- 세계관: targetId, expectedVersion과 기존 WorldSettingPropertyUpdateRequest를 받는다.
  기준의 대상 identity·전체 properties·version과 실제 현재값을 대조하고 기존 서비스가 current scope/path,
  목적 scope/path·충돌·수동 보호를 검증한다. 영수증은 실제 저장한 before/after와 version을 기록한다.
  old/new 경로는 편집 전후 `WorldSetting.getStoredPropertyPath`가 반환한 실제 저장 이름을 사용한다.
  공백·대소문자·NFC alias 요청 문자열을 그대로 정정 scope에 저장하거나 별도 정규화기로 다시 해석하지 않는다.

현재 baseline과 일치하지 않는 과거 항목, worldHistory, 다른 대상, 모호한 범위와 stale version은
`VALIDATION_CORRECTION_CONFLICT` 또는 report version 충돌로 거절한다. BASELINE_ONLY 철회는 지원하지 않는다.
일반 저장은 기존 report를 무효화할 뿐 직접 정정 receipt를 생성하지 않는다.
DB receipt 저장이 실패하면 실제 캐릭터 Fact/snapshot/provenance·세계관 값·baseline 무효화까지 모두 롤백한다.

새 seed와 해당 항목의 `state.explicitAuthorCorrections`에는 서버가 검증한 target/slot 또는 전체 scope/path,
before/after 값·entityVersion·정정 ID·기준 entry/hash/revision만 전달한다. 클라이언트가 의미 범위를 고르지 않는다.
명시적 정정의 연속 편집은 이미 검증한 이전 정정 scope를 서버에서 연결해 보존한다.
일반 저장이나 출처 파기 뒤에는 이 복사본으로 이전 주장을 되살리지 않는다.
Python의 GH306 단수 `explicitAuthorCorrection`은 자유 문자열 질문 scope였으므로 실행 연결에서
이 typed 경로 metadata를 실제 검사 항목에 매핑해야 한다. 임의의 전체 인물 예외로 확대하면 안 된다.

## 진단 재검수

`previousApplicationReceipt`는 실제 `automaticAppliedAt` Job, CONFIRMED 캐릭터 후보 또는 CONFIRMED 세계관 후보 ID를
구분한다. 단순 SUCCEEDED를 확정으로 추정하지 않는다. 캐릭터 후보에는 별도 확정 시각이 없어 `appliedAt=null`이다.
개별 후보 확정은 해당 이력의 증거이며 회차 전체 APPLIED 완료를 뜻하지 않는다.
Job이 없는 legacy 캐릭터 확정도 직접 회차의 실제 후보 ID를 보존하고 null Job receipt로 진단을 보류한다.
해당 원고/target membership의 PENDING/RUNNING 추출·비교 Job이 있으면 재검수를 거절해 진행 중 실행을 끊지 않는다.

검증 가능한 확정 전 입력이 있을 때만 진단 기준을 준비한다. 현재 지원하는 이력은 hash가 일치하는
SEALED ordered 자동 입력 또는 첫 순번 수동 runBaseState다. 현재/후행 회차 provenance가 있으면 거절한다.
Work 잠금 안에서 그 입력에 저장한 출처 ID/hash와 실제 후보·Job의 저장 위치·현재 수명도 검증한다.
참조 회차의 파기가 대기 중이면 retained 원문 key가 있어도 진단을 보류한다. 완료된 후행 Job의 이전 입력도
지연 파기 때 redacted될 수 있기 때문이다. 현재 원문의 manifest로 과거 출처를 다시 연결하지 않는다.
이 진단 전용 검증은 새 원문에서 직접 캡처한 일반 기준의 retained-key 허용을 바꾸지 않는다.
그 입력의 실제 정정 receipt가 있다면 검증한 단일 slot/path만 순서대로 정정하며 다른 live 현재값을 섞지 않는다.
확정 전 자료가 없거나 입력 출처가 파기되어 hash를 검증할 수 없으면
`HELD_INCOMPLETE + DIAGNOSTIC_BASELINE_UNAVAILABLE`로 남긴다. 현재 설정 fallback이나 과거 상태 추측을 하지 않는다.
따라서 일부 legacy 진단은 기준을 준비할 수 없다. report 자체가 없는 기존 분석 회차의 새 진단 입구도 이번 범위가 아니다.
진단은 이전 확정 사실을 보존하며 새 추출·후보·확정·자동 철회를 하지 않는다.

## 조회·무효화·파기 경계

현재 report를 선택하는 소비자는 `ValidationReportRepository.findByEpisodeIdAndCurrentTrue`,
`findStatesByEpisodeIdIn`(Episode 목록), `AnalysisBatchSummaryRepository.readValidations`(배치 요약),
`ValidationDataServiceImpl.requireCurrentReport`(읽기 전후 scalar 검증)다.
호환 `findByEpisodeId`도 현재 report만 반환한다. 배치 요청 이력의 min/max 시각 SQL은 모든 접수를 집계하고,
`AnalysisJobEligibility`·`AnalysisJobRepository.existsValidationStagingForEpisodes`의 추출 차단은 모든 이력을 검사한다.
이력 수를 현재 검수 수로 합치거나 기존 Job 성공 수로 가장하지 않는다.

`ValidationStagingEvents.ReportInvalidated(UUID workId, UUID reportId)`는 Work 잠금 안의 동기 이벤트다.
`ValidationBaselineService`가 seed/entries/sources를 지운 뒤 발행한다. 후속 Job·checkpoint·결과·인용 소유자는
같은 트랜잭션에서 이 이벤트를 소비해 무효화/본문 삭제를 연결해야 한다. source·setting 변경과 퇴역이 같은 경계를 사용한다.
작품/회원 파기는 결과/check history/evidence/checkpoint/publication → 검수 usage → 검수 Job → correction/baseline source/request key → report 순서로 자식을 지운다.

파일 교체 파기 요청의 retainedContentKey는 새 원문을 보존한다. `blocksSource(episodeId,key)`는 삭제/다른 key만 막고,
늦은 정리의 `EpisodeSourceChanged(workId,episodeId,retainedContentKey)`도 보존 key의 새 report를 지우지 않는다.
고정 contentRevision·hash/key/version 검증은 별도로 유지해 같은 key의 과거 report를 복구하지 않는다.

고정 자료 응답은 실제 baselineRevision을 반환한다. GH306 Python client의 `baselineRevision == 1` 고정 검증은
실행 연결에서 신뢰된 claim의 expected revision 검사로 바꿔야 한다. 최초 접수/read fixture의 revision 1은 유지한다.
새 manifest의 회차 contentRevision도 검색·읽기·게시의 동일 입력 검증으로 전달해야 한다.

## 로컬 검증

H2 focused: `./gradlew test --tests '*ValidationRecheckIntegrationTest' --tests '*ValidationCorrectionIntegrationTest'`.
PG 테스트는 새 loopback `gh310_recheck_test` DB에서만 실행하며 .env/공유 DB를 사용하지 않는다.
`GH310_RECHECK_POSTGRES_JDBC_URL=jdbc:postgresql://127.0.0.1:<port>/gh310_recheck_test`를 주입하고
`./gradlew test --tests '*ValidationRecheckPostgresIntegrationTest'`를 실행한다.
사용자는 `gh310`, 암호는 합성 fixture 전용 `gh310-disposable-only`다. 전체 Flyway 적용 후 JPA validate로 기동한다.
테스트는 fixture마다 works/members를 CASCADE TRUNCATE하므로 해당 일회용 DB 외에는 실행하지 않는다.
실제 모델 호출이나 운영 배포 검증이 아니며 전체 Java/AI 통합 gate는 마지막 구현 단위에서 별도로 수행한다.


## 전용 Job과 실행 API

`ValidationJob`은 report당 하나인 실제 실행/과금 소유자다. AnalysisJob을 가장하거나 생성하지 않는다.
claim은 짧은 Work → Job 잠금 뒤 입력과 상태를 다시 확인하고 무작위 lease token을 발급한다.
실행 세대 `generation`은 회수/명시 재시도마다 바뀌며 `attempt`는 실제 claim마다 증가한다.
report ID/revision, target contentRevision/hash/key/version, baseline revision/hash, manifest hash로 만든
서버 `inputDigest`는 같은 입력에서 유지한다. 각 질문의 `checkInputDigest`와 revision은 별도 CAS다.
이 값들은 Worker가 재계산하지 않는 불투명 서버 토큰이고 모델 인자가 아니다.

모든 내부 응답은 `CommonResponse.data`이며 exact JSON·null 필드·hash vectors는 공통 fixture의 `execution`에 있다.
`X-Internal-Api-Key`를 모든 내부 경로에, claim 이후에는 `X-Worker-Lease-Token`을 함께 보낸다.

| 경로 | 요청/결과 |
| --- | --- |
| `POST /api/internal/v1/validation-jobs/claim` | contractVersion=1, workerId, VALIDATION_EXECUTION_V1 capability. 대기가 없으면 data=null |
| `POST /{jobId}/heartbeat` | Scope. 실제 현재 lease 연장, reportVersion 증가 없음 |
| `GET /{jobId}/checkpoint` | 현재 lease의 저장 stages/checks/분기 checkpoints/usage와 budgetRevision/history |
| `POST /{jobId}/plans` | Publish: Scope, report별 idempotencyKey, branch, stage/ref, 의미 payload, citations, nullable failureCode |
| `POST /{jobId}/units` | Unit: Scope, branch/ref, 현재 check ID/revision/digest 또는 typo segment, outcome/payload/citations |
| `POST /{jobId}/revisions` | Revise: 영향 check 전부의 CAS·새 lookup_obligations·변경 사유. 전체 성공 또는 전체 rollback. grid v4 Worker는 호출하지 않는다 |
| `POST /{jobId}/checkpoint` | Checkpoint: branch, 그 분기의 expectedVersion, 의미 payload/citations. grid v4 Worker는 호출하지 않는다 |
| `POST /{jobId}/complete` | Complete: Scope, key, nullable 안전한 failureCode. 서버가 저장 처리 범위로 상태 결정 |
| `POST /{jobId}/usage/reserve` | Reserve: 실제 call requestId, role/model/raw 예약량 |
| `POST /{jobId}/usage/{requestId}` | Usage: SETTLE/UNKNOWN/RELEASE, 원래 예약 Scope와 lease |

표의 `/{jobId}` 접두사는 `/api/internal/v1/validation-jobs`다.
Complete의 `failureCode`는 wire 형태를 바꾸지 않고, 실패한 분기의 가장 구체적인 안전 코드(`[A-Z][A-Z0-9_]{0,99}`)를 보낸다(GH331).
설정 분기는 Python 분류 코드(`INVALID_INSPECTION_CONTRACT`, `INVALID_DECISION` 등) 대신 원인 코드(`INVALID_EXACT_SPAN`, `INVALID_DECISION_REQUIRED_READS` 등)가 있으면 그것을 보내며,
분류·단계·정규화 횟수는 Worker 로그에 남긴다. 원문·예외 메시지·URL은 넣지 않는다. Backend는 기존처럼 저장만 하며 해석하지 않는다.
질문 하나만 INCOMPLETE로 남긴 SETTING 분기는 `SETTING_QUESTIONS_INCOMPLETE`를 보내고 질문별 원인 코드는 그 질문의 INCOMPLETE unit에 남긴다(아래 "질문 하나의 INCOMPLETE").
공개 `GET /api/v1/works/{workId}/episode-validations/{reportId}/results`는 상태·처리 수(`completedChecks`·`incompleteChecks`·`totalChecks`)·coverage 요약,
현재 check 요약(INCOMPLETE check는 `failureCode`·`failureCause`·`targetSpans` 포함)·finding·검증된 인용만 반환한다. 실행 중/중단 후 모두 읽기 전용이다.
내부 전체 evidence_bundle/catalogue/checkpoint/lease/호출 원장 상세는 공개 응답에 내보내지 않는다.
`POST /.../{reportId}/retries`는 expectedReportVersion과 idempotencyKey로 같은 입력을 다시 queue한다.

## 초기 호출과 서버 발급 모델 입력

grid v4(#349 개편, `projectionVersion=2`)의 초기 순서는 순환하지 않는다.

1. claim에서 서버 Scope/회원·작품·report revision, baseline/target/manifest hash, 실제 lease/attempt,
   `contractVersion=1`·`projectionVersion=2`·`configurationVersion=2`, 역할별 모델/추론 강도와 명시 budgets를 받는다.
   `clueVersion`은 없다. Worker는 `projectionVersion`이 2가 아니면 `UNSUPPORTED_EXECUTION_CONTRACT`,
   configuration v2가 아니거나 PLANNER·JUDGE·TYPO 중 하나라도 `{model, reasoningEffort}`·`roles[R].outputTokens`가 없으면
   `EXECUTION_BUDGET_CONFIGURATION_INVALID`로 시작하지 않는다. legacy 설정 Job의 claim은 `configurationVersion: null`이다.
2. claim.checkpoint 또는 GET checkpoint로 이미 저장된 단계/check를 먼저 복원한다. 초기 requiredGrid=null은 정상이다.
3. data 경로의 INPUT, 실제 T1 전문 READ_SOURCE(`modelClues` 없음), SUBJECTS를 한 번씩 읽는다. 전용 Job이 있으면 기존 회원 헤더에
   `X-Validation-Job-Id`와 실제 lease를 반드시 추가한다. 모든 data 응답은 `projectionVersion=2`이고 `clueVersion`이 없다.
4. SUBJECTS는 페이지 없이(offset/limit 무시) `{"subjects":[{"subject_ref","domain","name","aliases"}],"subjectCount","entryCount"}`를
   S 번호 순으로 준다(`ValidationDataMapper.toValidationSubjectCatalogue`). 묶음 규칙은 AI `subject_catalogue()`와 같다: 한 주체의 첫 항목이
   domain·name을 정하고 aliases는 그 주체 항목 전체의 순서 보존 합집합이다(공통 fixture `subjectCatalogueVector`).
5. 계획 뒤 JUDGE 전에 SETTINGS를 요청 하나에 최대 100개 주체(`subjectRefs`, `S[1-9][0-9]*`, 비어 있으면 안 되고 모두 고정 기준에 있어야 함,
   위반은 `REQUEST_VALIDATION_FAILED`)로 읽는다. 응답 `settings`는 기존 정본 projection 행(`entry_ref, subject_ref, domain, name, aliases,
   state, author_correction:null`) 중 요청 주체이면서 `domain ∈ {characters, worldSettings}`(`ValidationDataMapper.CURRENT_DOMAINS`)인 행만
   B 번호 순으로 담는다. `worldHistory` 행은 돌려주지 않는다. `authorCorrectionProofs`는 그 행들의 typed 정정 사실 전송이다.
6. ENTRIES는 legacy 연산으로 남지만 Worker는 쓰지 않는다. VALIDATE는 그대로이며 서버가 모든 게시 안에서 호출하고 TYPO가 마지막에 쓴다.
7. 각 PLANNER 단계를 검증 즉시 게시하고, 서버 v4 grid의 segments가 로컬 분할과 같은지 확인한 뒤 MERGED_PLAN의 안정 check
   ID/revision/digest를 받고 JUDGE를 시작한다.

projection은 기존 값·조건·시점을 보존하고 서버 ID/hash/key/token metadata만 모델 투영에서 제거한다.
원문 provenance가 정확히 고정 manifest와 일치할 때만 R-ref를 추가한다. canonical author_correction은 null이다.
정정 기록은 실제로 존재하지만 질문의 자유문자열 서사 시점/조건 권위로 추론하지 않는다.
이 계약에서 DB_RAW_DISAGREEMENT는 NEEDS_CONTEXT이며 #312의 작가 판단으로 검토할 수 있다.
정확한 typed 시간·조건 대응이 입증되는 후속 계약 전에는 자동 NO_CONFLICT 승격이나 광범위한 예외를 만들지 않는다.

data 요청(`ValidationDataRequest`)의 operation은 `INPUT|ENTRIES|SUBJECTS|SETTINGS|READ_SOURCE|VALIDATE`이고 마지막 필드로
`subjectRefs`(최대 100, SETTINGS 전용)가 추가됐다. READ_SOURCE는 더 이상 `modelClues`를 계산하거나 돌려주지 않는다(`ValidationClueCollector` 삭제).

## 의미 단계와 완료 증명

서버가 실제 전문 길이와 고정 `segmentChars`·`contextChars`로 필수 grid를 계산한다. Worker가 required-grid 또는 done:true를 보내 완료를
대체할 수 없다. SETTING grid는 **`gridVersion=4`**(`ValidationPublicationValidator.SETTING_GRID_VERSION`), TYPO grid는 1 그대로다.

```json
{"gridVersion":4,
 "segments":[{"ref":"TSEG1","start":0,"end":7408,"contextStart":0,"contextEnd":7408}],
 "units":[{"stage":"PLANNER","ref":"TSEG1"},{"stage":"MERGED_PLAN","ref":"PLAN"}]}
```

grid v4에는 `segmentSubjects`, `clueVersion`, `modelClues`, `books`, `pendingBookRefs`와 SUBJECT_RESOLUTION·COVERAGE·BOOK_SOURCE·
BOOK_COVERAGE 의무가 없다. 게시 준비는 INPUT과 T1 전문만 읽으며 설정집은 RAG 색인이 생길 때까지 직접 읽지 않는다(설정집 원문 변경·파기는
여전히 VALIDATE와 고정 manifest 검증으로 충돌·무효화된다). `extendGrid`는 두 분기 모두 저장 grid와 다시 계산한 grid를 엄격히 비교한다.

구간 분할(`ValidationPublicationValidator.settingSegments`, Java·Python 동일, 정수 연산만, `U=segmentChars`, `C=contextChars`, `L`=T1 code point 길이):

```text
n = ceil(L/U) = (L+U-1)//U                    # L<=U 이면 n=1, 구간 [0,L)
k = 1..n-1:
  t  = (k*L)//n
  lo = ((2k-1)*L)//(2n)                        # 제외
  hi = ((2k+1)*L)//(2n)                        # 포함
  cand = { p | lo < p <= hi and codepoint[p-1] == U+000A }   # '\n' 바로 뒤에서 자름
  cut_k = argmin over cand of (|p-t|, p)                      # 동률은 작은 p
  cand가 비면 cut_k = max(t, lo+1)
bounds = [0, cut_1..cut_{n-1}, L]
TSEGi: start=bounds[i-1], end=bounds[i], contextStart=max(0,start-C), contextEnd=min(L,end+C)
```

창 `(lo,hi]`는 겹치지 않고 `L ≥ n`이라 항상 `hi > lo`이므로 자른 위치는 엄격히 증가하고 빈 구간이 없다. `\n`만 줄바꿈으로 보므로 `\r\n`은
쪼개지지 않는다. 공통 fixture `gridCases`가 L≤U, U의 배수, 창 안 줄바꿈 없음(fallback), 같은 거리 줄바꿈 동률, `\r\n`, BMP 밖 문자,
양 끝 문맥 잘림, U=1·2, 25,000자 3구간(`targetRepeat`: `unit`을 `count`번 반복한 대상) 벡터를 담고 `ValidationExecutionContractTest`와
AI `tests/test_validation_segmentation.py`가 같은 파일을 읽는다. TYPO는 기존 고정 분할이다.

- PLANNER(`/plans`, stage=PLANNER, ref=TSEGn): `{"questions":[QuestionDraft...],"segments":[{"segment_ref":"TSEGn","status","reason"}]}`.
  `rules`·`clue_checks`와 초안의 `lookup_intents`는 없어야 한다. 모든 span은 `start ≥ seg.start`, `end ≤ seg.end`인 핵심 구간 안이며
  읽기 전용 문맥 안의 span은 거절한다. `status == PLANNED` ⇔ questions가 비어 있지 않다.
- MERGED_PLAN: `questions[{localRef, originalRefs:["PLANNER:TSEGn:<local_ref>"...], state:{draft, lookup_obligations:[]}}]`. 원 질문은 PLANNER뿐이며
  병합 키(subject_refs 집합, property_or_rule, time_context, conditions 집합, assertion_mode)는 그대로다. `lookup_obligations`가 없으면 `[]`로 저장한다.
- TYPO_SEGMENT: 분류(SPELLING/SPACING/TYPO)와 정확한 제안 또는 제안 0개의 완료 segment.
- 이전 grid의 단계 종류(RELATED_SELECTION, SUBJECT_RESOLUTION, COVERAGE, BOOK_SOURCE, BOOK_COVERAGE)는 v4 grid 단위가 아니어서 게시하면
  VALIDATION_RESULT_INVALID다. `ValidationStageKind` 상수는 저장 행 때문에 유지한다.

각 DECIDED unit에는 `ValidationDecision`(`follow_ups: []`, `affected_question_refs: []`)과 `evidence_bundle`을 함께 보낸다.
bundle은 `{question_ref, check_revision, question(draft), target_context, additional_target_context, settings:[{entry_ref,subject_ref}],
evidence, receipts, source_catalogue, input_digest}`이며 `question_refs`는 없다. question_ref/check_revision/question은 저장 질문과 일치해야 한다.
서버는 settings ref 집합이 질문 subject_refs의 항목 중 `domain ∈ {characters, worldSettings}`인 항목(BASELINE 의무 ref는 v4에서 항상 없음)과 같은지,
실제 E-ref/원문 위치, target_context·additional_target_context 인용, 알려진 fact/evidence refs, 성공한 receipts를 검증한다.
receipt intent 연산은 `{BASELINE, TARGET_CONTEXT, VECTOR}`만 허용한다(`ValidationDecisionValidator.INTENT_OPERATIONS`). SUSPECTED_CONFLICT는
`completion.changes_exceptions.receipt_refs`에 `kind=CHANGES_EXCEPTIONS`이고 **`operation=VECTOR`**인 성공 receipt가 있어야 한다(KEYWORD 없음).
네 completion 사유만으로 완료하지 않는다. 정상 NO_CONFLICT도 완료 check/receipts를 저장한다. E-ref는 실제 source_ref로 분류한다.
T1을 E1로 포장해도 CONFLICT/SUSPECTED_CONFLICT의 외부 비교 근거가 될 수 없고 frozen fact 또는 검증된 T1 이외의 source만 인정한다.

bundle `settings[]` 행은 `{"entry_ref": "B12", "subject_ref": "S3"}`다(#349 후속). `entry_ref`는 현재 고정 기준의 항목이고
`subject_ref`는 그 항목의 canonical `subject_ref`와 같아야 하며 ref는 중복되지 않는다. 다른 키는 무시한다. bundle은 공개 응답에 나가지 않고
서버는 ref 집합(finding 대상 계산)과 receipts만 쓰므로 내용 사본이 필요 없다(운영 캐릭터 항목 하나가 362 KB여서 전체 행 비교는 게시 상한을 넘겼다).

판정 흐름(공통 fixture `judgeFlow`, 질문당 최대 3회, 동시에 4개 질문):

1. OPEN: 질문 초안, span마다 앞뒤 300 code point의 `target_context`(Worker 상수 `JUDGE_TARGET_CONTEXT_CHARS`, 계획의 1,000과 별개),
   질문 주체의 압축 **현재값** 행. Worker는 `BASELINE/COMPARISON_CONTEXT`(evidence_refs = 그 B ref)와 `TARGET_CONTEXT/COMPARISON_CONTEXT`
   receipt를 만든다. 답은 DECIDED, 또는 `search_queries`(자연어 1~1000자 최대 3개, 검색을 쓸 수 없으면 schema에서 빠짐)와/또는
   `context_requests`(T1 `{start,end}`)를 담은 FOLLOW_UP이다.
2. FOLLOW_UP 뒤: 검색 문장마다 VECTOR 한 번(top-k `searchLimit`, intent `{kind:COMPARISON_CONTEXT, operation:VECTOR, purpose:"판정이 요청한 의미 검색", query}`),
   문맥 요청은 TARGET_CONTEXT receipt(조회 0회). 검색 문장이 하나 이상이면 그 뒤에 Worker가 대상 문장 검색 하나를 더 한다(2026-11-03,
   `{ref:"Qn-TARGET_VECTOR", kind:COMPARISON_CONTEXT, operation:VECTOR, purpose:"Worker가 대상 문장과 앞뒤 원고로 만든 이전 장면 의미 검색",
   query: span과 앞뒤 100 code point를 줄 단위로 맞춘 대상 원고 글}`, 판정의 3개와 별도, 조회·embedding 1회 또는 같은 문장 재사용).
   다음 호출은 FINAL(DECIDED만).
3. 호출 한도 전의 DECIDED SUSPECTED_CONFLICT에 변화 검색 receipt가 없으면 Worker가 `{kind:CHANGES_EXCEPTIONS, operation:VECTOR,
   purpose:"Worker가 충돌 의심 판정 전에 요구하는 변화·예외 검색", query: check_intent + 대상 span quote(2026-11-03, 이전에는 subject_mentions +
   property_or_rule)}` 검색 하나를 실행하고 다음 호출을 FINAL로 한다. 세 번째 호출은 이 경로로만 가므로 최종 SUSPECTED_CONFLICT에는 항상 그 receipt가 있다.
   공통 fixture `judgeFlow.calls[2]`의 query 설명도 이 규칙으로 고쳤다(2026-11-03 headline 동기화 때, 서버는 query 내용을 검사하지 않음).
3-1. 판정 규칙 검사(2026-11-03, `judging.clear_unquoted_finding`): Worker가 받아들이는 CONFLICT·SUSPECTED_CONFLICT에 확인된 `basis_quotes`가 하나도 없고
   `disagreements`도 없으면 NO_CONFLICT/COMPATIBLE(impact·impact_reason null, 고정 사유)로 게시한다. 질문 실패가 아니고
   `CONFLICT_QUOTE_MISSING_CLEARED`/`SUSPECTED_CONFLICT_QUOTE_MISSING_CLEARED`로 센다. 게시 형식은 기존 NO_CONFLICT와 같아 서버 변경이 없다.
4. 한 실행 안의 같은 검색(공백을 뗀 문장)은 재사용한다. 새 L-ref에 `reused_from`을 달고 조회·embedding을 쓰지 않는다.
5. 과거 회차 출처가 없으면 VECTOR는 근거 없이 성공하고 `readiness.vector_status=NOT_APPLICABLE`이며 embedding을 만들지 않는다.
   회차가 있는데 RAG 색인이 완전하지 않으면 검색을 제안하지 않고, 필요한 검색은 그 질문을 `VECTOR_NOT_READY`로 끝낸다(아래 INCOMPLETE).
6. KEYWORD·DIRECT·NEIGHBORS, BASELINE 확장·이력 행, `affected_question_refs` 재개, `/revisions`, JUDGING runtime checkpoint는 없다.
   `/revisions`·`/checkpoint` 경로와 기존 FOLLOW_UP revise 규칙(영향 check 전부 원자 revise, 옛 완료는 check history에 남김)은 서버에 그대로 있으나
   grid v4 Worker는 호출하지 않는다.

### 판정 근거 문장(basis_quotes)과 작가용 문장 (2026-11-02)

오류 리포트 상세가 "어디를 봐야 하는지" 보여 줄 수 있도록 DECIDED 판정은 근거로 삼은 비교 쪽 문장을 정확히 인용한다(소유자 피드백, 31화 로컬 리포트).
schema 이름(`validation_judge_open_v3`·`validation_judge_final_v3`)은 그대로이고 같은 v3 안에서 필드를 추가했다(v0045, 미배포).

- 판정 답(OPEN·FINAL 모두 필수): `basis_quotes: [{ref, quote}]` 0~3개. `ref`는 `E#` 또는 `B#`, `quote`는 그 evidence 조각 또는 저장 설정 값에서
  글자 그대로 복사한 문장·구절(schema 1~300자, 프롬프트는 200자 이하)이다. T1 문장은 넣지 않는다. 근거 문장이 없으면(저장된 설정에 이 내용이 없는 경우 등) `[]`,
  FOLLOW_UP의 값은 무시한다(`FOLLOW_UP_DECISION_FIELDS_IGNORED`).
- Worker 검증(`judging.normalize_basis_quotes`, 모델 순서, 최대 3개): 앞뒤 공백을 떼고 1~300 code point여야 한다.
  - `E#`: 그 질문 bundle evidence 행의 `quote` 안 첫 위치(없고 quote에 LF, 조각에 CRLF가 있으면 LF를 CRLF로 바꿔 한 번 더). 행의 `source_ref`가 T1이면 버린다.
    게시 값은 원 출처의 절대 위치 `{ref, source_ref: 행.source_ref, start: 행.start + index, end: start + 길이, quote: 조각의 정확한 부분 문자열}`(code point)이다.
  - `B#`: 그 질문 bundle settings 행의 **설정 값 leaf** 중 하나의 부분 문자열이어야 한다. leaf는 `characters`면 `state.slots.*.factValue` 문자열,
    `worldSettings`면 `state.propertiesJson` 아래 모든 문자열(객체 값·배열 항목을 재귀, 값 자체가 문자열이면 그것)이다. key·이름·별칭·다른 domain은 인용할 수 없다.
    게시 값은 `{ref, source_ref: null, start: null, end: null, quote}`다.
  - 알 수 없는 ref, 못 찾은 문장, 길이 초과, 같은 `(ref, quote)` 중복, 4번째 이후는 버리고 `BASIS_QUOTES_DROPPED`로 센다. 질문 실패로 만들지 않는다.
- 게시: `ValidationDecision.basis_quotes`(이전에 게시된 결정에는 없으며 `[]`로 읽는다). E 인용은 checkpoint의 citation 수집기가 다른 span처럼
  `citations`에 `{sourceRef, sourceHash, sourceVersion, start, end, quote}`로 보내 서버가 hash 확인한 전문을 code point로 잘라 비교한다.
  B 인용은 원문이 아니어서 citation이 없고, 서버는 고정 기준 항목 투영(`toValidationSettings`)의 같은 leaf 규칙으로 부분 문자열인지 확인한다.
  공통 fixture `judgeFlow.basisQuotes`(규칙·`vectors`)와 `payloadCitationPaths.basisQuotes`를 Java·Python이 같이 읽는다.
- 작가용 문장(`app/validation/author_text.py`, fixture `judgeFlow.authorText`): Worker는 저장 전에 `headline`(2026-11-03, 아래)·`reason`·`impact_reason`·`disagreements[].reason`에서
  내부 참조를 작가 말로 바꾼다. `T1`·`TSEG#`→"이번 원고", `E#`/`R#`→"N화"(설정집은 "설정집"), `B#`→"저장된 설정", `S#`→SUBJECTS의 대상 이름(없으면 "대상")이며
  바로 뒤 조사(은/는, 이/가, 을/를, 과/와, 으로/로)를 받침에 맞춘다. 그래도 모르는 참조(L#, Q#, 질문 밖 E/B/R/S)나 내부 용어(limited, receipt, readiness, vector,
  baseline, target_context, keyword, evidence_refs, receipt_refs, source_ref, evidence_bundle, rag, 영수증)가 남은 문장은 버리고, 다 버려지면 판정·영향 등급별
  고정 문장을 쓴다. 바뀐 필드마다 `AUTHOR_TEXT_SANITIZED`로 센다. 참조·용어가 없는 문장은 글자 그대로 둔다. `completion.*.reason`은 감사 기록이라 바꾸지 않으며
  공개 상세 화면에도 쓰지 않는다. 판정 프롬프트는 같은 규칙(80자 안팎의 쉬운 한 문장, 참조·내부 용어 금지)을 먼저 요구하고 이 정제는 안전망이다.

### 판정 작성 제목(headline)과 원인→결과 사유 (2026-11-03)

오류 리포트의 제목·설명을 화면이 대상 이름·`property_or_rule`·첫 인용으로 조립하면, 질문 첫 주체(예: 서술자)가 제목에 오르고 대상 span이 둘이면
문맥 쪽 문장을 인용하는 문제가 있었다(소유자 피드백, 9등급을 심은 31화). 그래서 제목과 설명을 판정이 쓴다. schema 이름은 그대로 v3이다(v0045, 미배포).

- 판정 답(OPEN·FINAL 모두 필수, nullable): `headline`. DECIDED의 작가용 제목 한 줄(40자 안팎, 쉬운 말, 참조·내부 용어 금지).
  CONFLICT `"{대상}의 {항목}이 기존 설정과 달라요"`(어긋나는 이전 문장이 이전 회차면 `…N화와 달라요`, 설정집이면 `…설정집과 달라요`),
  SUSPECTED_CONFLICT `…다를 수 있어요`, NEEDS_CONTEXT는 무엇이 분명하지 않은지(`‘그’가 누구인지 확인이 필요해요`). {대상}은 저장 값·이전 문장이 실제로
  어긋나는 대상(서술자·이름만 나온 인물이 아님), 여럿이면 `A와 B`. FOLLOW_UP·NO_CONFLICT는 null.
- `reason`: CONFLICT·SUSPECTED_CONFLICT는 원인 → 결과 두 문장(120자 안팎) `기존 설정은(또는 N화에서는/설정집은) {대상}의 {항목}을 {기준 값}(이)라고 해요.
  그런데 {대상 회차}에서는 {이번 값}(이)라고 해요.`(값은 짧게 바꿔 씀). NEEDS_CONTEXT·NO_CONFLICT는 80자 안팎 한 문장 그대로.
- 판정 입력: 모델 payload에 `target_episode_no`(INPUT `target.episodeNo`, 없으면 null)를 더한다. evidence bundle(키·`input_digest`)은 그대로다.
- Worker(`author_text.sanitize_decision`): FOLLOW_UP의 값은 무시(`FOLLOW_UP_DECISION_FIELDS_IGNORED`), NO_CONFLICT는 null(`NO_CONFLICT_HEADLINE_CLEARED`),
  앞뒤 공백 제거, 공백뿐이면 null, `reason`과 같은 작가용 정제(`AUTHOR_TEXT_SANITIZED`) 뒤 남은 것이 없거나 80 code point를 넘으면 null(`HEADLINE_DROPPED`).
  고정 대체 문장은 없다(화면이 자체 제목을 쓴다). 충돌·의심·추가 확인이 null로 끝나면 `HEADLINE_MISSING`. 판정 규칙 검사(근거 문장 없는 충돌 → 문제없음)와
  DB_RAW_DISAGREEMENT의 NEEDS_CONTEXT 재작성은 headline을 null로 둔다. 질문 실패를 만들지 않는다.
- 게시: `ValidationDecision.headline`(이전 결정에는 없으며 null로 읽는다). 서버는 없거나 null을 받고, 문자열이면 공백 제거 뒤 비어 있지 않고 80 code point 이하여야 하며
  NO_CONFLICT의 non-null은 `VALIDATION_RESULT_INVALID`다. 상세·목록 응답 `headline`(이전 결과는 null). 공통 fixture `judgeFlow.headline`(`vectors`=서버,
  `workerCases`=Worker)을 Java·Python이 같이 읽는다.

### 질문 하나의 INCOMPLETE (#349 결정 ③, 2026-11-02)

질문 단위 오류(양식·계약 오류, 출력 절단, 잘못된 결정·FOLLOW_UP, 검색 실패, `VECTOR_NOT_READY`, `VECTOR_INDEX_PARTIAL`,
`MODEL_CALL_FAILED`, `REQUIRED_CONTEXT_TOO_LARGE` 등, AI `app/validation/failures.py`의 `QUESTION_LEVEL_CODES`)로 한 질문이 결론을 못 내면
Worker는 **그 질문만** INCOMPLETE unit으로 게시하고 나머지 질문을 계속 판정한다. 실행 단위 오류(한도, 입력·출처 변경, gate 중단, 사용량,
lease/HTTP, 자료 접근)는 지금처럼 새 판정 시작을 멈추고 진행 중 판정만 끝내고 게시한다. INCOMPLETE 게시 자체가 실패해도 실행 단위 중단이다.

```json
{"branch":"SETTING","ref":"Q1","checkId":"…","expectedCheckRevision":1,"checkInputDigest":"…","outcome":"INCOMPLETE",
 "payload":{"action":"INCOMPLETE","outcome":"INCOMPLETE","failure_code":"MODEL_CONTRACT_INVALID","failure_cause":"MODEL_CONTRACT_JSON_INVALID",
            "failed_call":1,"target_spans":[{"source_ref":"T1","start":0,"end":2,"quote":"😀가"}]},
 "citations":[{"sourceRef":"T1","sourceHash":"…","sourceVersion":"…","start":0,"end":2,"quote":"😀가"}]}
```

- 서버 검증(`ValidationResultServiceImpl.incomplete`): payload key는 위 6개뿐, action·outcome은 INCOMPLETE, `failure_code`는 안전 코드,
  `failure_cause`는 null 또는 안전 코드, `failed_call`은 0~3, `target_spans`는 그 check 초안의 span 집합과 같고 일반 span처럼 인용 검사를 받는다.
  공통 fixture `judgeFlow.questionFailure`에 규칙과 예시가 있다.
- check는 outcome INCOMPLETE로 저장되지만 `ValidationCheck.completed()`가 아니다. finding을 만들지 않고, 그 실행은 COMPLETED가 될 수 없다.
- Worker는 SETTING 분기가 판정할 수 있는 질문을 모두 결정·INCOMPLETE로 끝냈으면 Complete `failureCode=SETTING_QUESTIONS_INCOMPLETE`를 보낸다.
  실행 단위 SETTING 코드나 TYPO 분기 실패 코드가 있으면 그것이 우선한다. 서버 상태는 새 enum 없이 기존 규칙(일부 성공 PARTIAL, 성공 0이면 FAILED)이다.
- 공개 results는 `incompleteChecks` 수와 INCOMPLETE check마다 `failureCode`·`failureCause`·`targetSpans`(검증된 인용)를 준다. 작가는 결정된
  finding과 함께 검사하지 못한 문장과 이유(안전 코드)를 본다. 표시 문구·멈춘 이유 화면은 후속 Front 범위다.
- 작가 [다시 시도](같은 입력 retry)는 INCOMPLETE와 미게시 질문만 같은 check revision에서 첫 판정부터 다시 하고, 게시된 결정은 그대로 둔다
  (AI checkpoint가 INCOMPLETE check를 미게시로 복원하고 서버는 completed()가 아닌 check에 새 결론을 덮어쓴다). Worker lease 회수도 같은 실행
  cap 안에서 INCOMPLETE 질문을 다시 판정한다.

### 종료 상태

종료 상태는 현재 revision의 완료 check(NO_CONFLICT 포함, INCOMPLETE 제외), 완료 typo segment(제안 0개 포함),
필수 PLANNER가 저장되고 미해결 질문이 없는 NO_CHECK segment로 결정한다. 계획/원문 읽기만 성공한 것은 성공 결과 수에 넣지 않는다.
성공 처리 0+미완료는 FAILED, 일부 성공+필수 미완료는 PARTIAL, 양쪽 모든 grid와 현재 질문이 완료되어야 COMPLETED다.
검토 종료 전에는 어느 경우에도 추출 Job·후보·자동 확정이 0이다. 정상 0건 COMPLETED만 같은 트랜잭션에서 검토 종료 receipt와 release Job 하나를 만들고
`/complete` 응답에 `reviewCompletionId`·`extractionJobId`를 기록한다(추출 생성 조건이 막히면 receipt 없이 결과만 저장, GH312 S2).
작가 판단은 GH312의 별도 API(`docs/validation-review-v1.md`)로만 저장하며 실행 상태를 바꾸지 않는다.

### legacy grid(v1~v3)와 legacy 설정의 retry 전환

grid v1(RELATED_SELECTION page), v2·v3(GH349: `segmentSubjects`·SUBJECT_RESOLUTION·COVERAGE, v3는 1글자 name·alias 제외),
`modelClues`·books/pendingBookRefs는 저장된 기록의 읽기 전용 형식으로만 남는다. 서버는 게시마다 grid를 다시 계산하고 `extendGrid`가 엄격히
비교하므로 배포 전에 SETTING grid가 저장된 Job은 배포 뒤 첫 SETTING 게시가 VALIDATION_SOURCE_VERSION_CONFLICT로 끝난다.
작가의 공개 retry(`retryValidation`, PARTIAL·FAILED)는 저장 SETTING grid가 v4가 아니거나(`currentSettingGrid`) Job configuration이 legacy이면
(`ValidationExecutionConfiguration.isCurrent`가 false) 같은 트랜잭션에서 SETTING 분기 사본을 버린 뒤 기존 retry를 그대로 진행하고(공개 report version은
그 retry 전이에서 전진한다), 다음 실행의 첫 SETTING 게시가 v4 grid를 새로 만든다. 배포 전 미완료 run의 산출물이므로
**전환 시 설정 분기 finding과 그 판단은 삭제되고 재검사로 다시 생성된다(오탈자 분기는 유지).** 새 Worker는 v4가 아닌 grid를 만나면
`GRID_VERSION_UNSUPPORTED`, v4인데 segments가 다르면 `GRID_SEGMENTS_MISMATCH`로 MERGED_PLAN 전에 멈춘다.

- 지우는 것(FK 순서): SETTING finding의 작가 판단 이력 → 판단 → SETTING finding(#314 B2 대상·등급 사본 컬럼 포함) →
  SETTING plan·checkpoint receipt → TYPO 밖의 인용 사본(stage·`CHECK:`·`REVISION`·`CHECKPOINT:SETTING` owner) → check revision
  이력 → 질문 check → SETTING checkpoint → SETTING stage(v1 RELATED_SELECTION page 포함). receipt를 지우는 이유는
  Worker의 idempotency key가 본문 hash라서 같은 본문을 다시 보내면 옛 receipt가 재생되어 새 stage와 grid 갱신이 생기지 않기 때문이다.
  Job의 `requiredGrid`에서는 SETTING만 빼고 `gridReady=false`로 둔다.
- 남기는 것: TYPO 분기 전부(finding·판단·판단 이력·stage·checkpoint·receipt·grid), 정정 receipt(`validation_corrections`),
  check 판정 unit·revision·완료 receipt(옛 check ID를 담은 응답 사본이며 새 check와 key가 겹치지 않는다), 사용량 원장, report 자체의 상태.
  예약 해제·UNKNOWN 처리는 기존 retry와 같다. 역할 cap은 새 실행(run)부터 센다(아래 실행 설정과 사용량).
- 검토 종료 receipt가 있는 report는 retry 자체가 VALIDATION_REVIEW_ALREADY_COMPLETED로 막히므로 전환되지 않는다. 재검수 경로는
  바꾸지 않는다(같은 원고는 VALIDATION_RECHECK_INPUT_UNCHANGED).

아래 조회는 배포 전후 진단용이다(`setting_findings`는 전환 때 지워지고 재검사로 다시 만들어질 설정 finding 수다).

```sql
select j.id, j.report_id, j.status, j.configuration ->> 'configurationVersion' as configuration_version,
       (select count(*) from validation_findings f where f.report_id = j.report_id and f.branch = 'SETTING') as setting_findings
from validation_jobs j
where (coalesce(j.required_grid -> 'SETTING' ->> 'gridVersion', '4') <> '4' or j.configuration ->> 'configurationVersion' is null)
  and j.status in ('QUEUED', 'RUNNING', 'PARTIAL', 'FAILED');
```

## 게시·파기·재시도

게시 외피 전체는 1,048,576 UTF-8 bytes(#349 후속: MERGED_PLAN이 모든 구간의 질문 초안을 한 번에 담아 65개 질문이 옛 131,072 bytes를 넘겼다), citations는 1,000개, 의미 payload 깊이는 50을 넘기지 않는다.
target_spans/spans/evidence 행/typo span과 target_context/additional_target_context라는 계약 경로의 quote/text는
정확한 source/hash/version/code point 인용 외피가 필요하다. settings[] 행은 현재 고정 기준 항목의 entry_ref·subject_ref와
맞아야 한다. 서버 canonical projection 전체와 같은 행은 그 내부 도메인 속성 이름 text/quote를 span으로 해석하지 않고, 그 밖의 행은
추가 키도 일반 payload처럼 검사한다. 다른 임의 payload/checkpoint 경로의
text/quote는 거절하므로 검증되지 않은 복사본문을 추가 필드에 숨길 수 없다. canonical 설정도 깊이 상한을 지킨다.
인용은 고정 출처 identity(source_ref의 frozen contentHash·contentS3Version)와 같아야 하고, 출처마다 한 번 읽어 hash를 확인한 실제 전문을
code point로 잘라 quote와 비교한다(#349 개편: 판정 unit 하나가 과거 회차 근거 수십 개를 인용하므로 인용마다 범위 READ_SOURCE(저장소 전체 GET)를 하던 것을 없앴다).
I/O 전에 현재 lease/입력을 확인하고, Work 잠금 안에서 current/revision/contentRevision/lease/출처 metadata를 다시 검사한다(VALIDATE).
S3/provider I/O 동안 Work를 잠그지 않는다.
같은 report/key의 exact payload는 원 receipt를 반환하고 다른 payload는 409다. 이미 바뀐 check revision의 unit은
기존 key 재전송이라도 거절한다. 성공한 단계의 새 key exact 전송은 상태나 공개 version을 다시 바꾸지 않는다.

같은 입력 retry는 report/성공 stages/check ID를 보존하고 미완료(INCOMPLETE·미게시 질문 포함)만 새 generation으로 실행한다.
공개 새 retry는 최신 활성 회차만 허용한다. 원래 성공한 retry key는 새 행동이 아니므로 원 summary receipt를 반환한다.
유효 saved seed의 준비 중단은 원래 hash/fingerprint·target/source 수명을 검증하고 기존 materialize로 복구한다.
seedless/invalidated/purged 입력을 그 경로로 복원하지 않고 Task 1의 명시적 새 baseline 접수를 사용한다.

ReportInvalidated는 baseline과 같은 트랜잭션에서 작가 판단 이력/판단(finding보다 먼저)과 stage/check/history/finding/evidence/checkpoint/publication 및 grid의
복사본문을 삭제하고 실제 Job을 취소한다. 원장은 원래 소유권을 유지해 늦은 실제 정산을 받을 수 있다.
Work/회원 파기는 RESERVED/UNKNOWN 호출도 기존 worker drain에 포함하고 계정 hold를 해제한 뒤 FK 자식을 먼저 지운다.
물리 파기 완료 뒤에는 기존 Job/원장도 없으며 그 이후 정산 소유권을 새 Job에 옮기지 않는다.

## 실행 설정과 사용량

`validation.execution.enabled`는 기본 false다. 켜려면 lease-duration, max-lease-recoveries,
setting/typo의 model-calls/lookups/seconds/input-tokens/segment-chars/context-chars/search-limit와
roles의 PLANNER·JUDGE·TYPO(`max-calls`, `max-reserved-tokens`, `output-tokens`, `model`, `reasoning-effort`)와 선택 QUERY_EMBEDDING(`max-calls`,
`max-reserved-tokens`, 출력·모델·추론 강도 없음)을 모두 명시한다. `output-tokens`와 `catalogue-page-size`는 setting/typo에서 빠지고 출력 상한은
생성 역할별 값이 됐다(configuration v2). 모델 이름은 `[A-Za-z0-9._-]{1,100}`, 추론 강도는 `none|minimal|low|medium|high|xhigh`다.
퇴역 역할 RELATED_SELECTION·COVERAGE를 켠 설정은 명시 오류로 거절한다. enum 상수는 저장 원장과 binding 호환을 위해 남아 있어 실행이 꺼진 기동은
옛 키가 남아도 실패하지 않는다. production 수치 기본값을 YAML·Java에 만들지 않는다.
정확한 Map enum 키를 보존하는 외부 YAML 또는 SPRING_APPLICATION_JSON을 사용할 수 있다.
운영은 `deploy/validation-execution.prod.example.json`을 `jq -c`로 만든 한 줄을 `api.env`의 `SPRING_APPLICATION_JSON`에 넣고
`VALIDATION_EXECUTION_ENABLED=true`와 함께 켜며, 운영 Compose가 두 값을 backend에 그대로 넘긴다(#344, 절차는 `deploy/EC2_DEPLOYMENT.md`).
`ValidationExecutionProdExampleTest`가 예시 파일이 활성 검증을 통과해 바인딩되는지 확인한다.
예시의 QUERY_EMBEDDING cap과 `query-embedding-model: text-embedding-3-small`은 Worker가 `--query-embedding`으로
실행될 때만 쓰인다. 쓰지 않으면 두 값을 함께 빼도 된다(역할만 남기고 모델을 빼면 기동이 실패한다).
테스트 수치는 `src/test/resources/validation-execution-fixture.properties`이며 운영 권장값이 아니다.
비활성 앱은 예산 없이 정상 기동하며 과거 report를 시작 시 일괄 queue하지 않는다. 활성화 뒤 기존 waiting report는
명시 START/RECHECK의 동일 접수 복구 또는 공개 retry로 시작한다.

운영 예시(2026-11-02 결정 수치, 코드 기본값 아님):

```json
{"validation":{"execution":{
 "lease-duration":"60s","max-lease-recoveries":2,
 "setting":{"model-calls":200,"lookups":300,"seconds":3600,"input-tokens":80000,"segment-chars":10000,"context-chars":1000,"search-limit":10},
 "typo":{"model-calls":20,"lookups":20,"seconds":900,"input-tokens":20000,"segment-chars":2000,"context-chars":100,"search-limit":10},
 "roles":{
  "PLANNER":{"max-calls":10,"max-reserved-tokens":800000,"output-tokens":50000,"model":"gpt-6.1-terra","reasoning-effort":"medium"},
  "JUDGE":{"max-calls":200,"max-reserved-tokens":10000000,"output-tokens":20000,"model":"gpt-6-luna","reasoning-effort":"high"},
  "TYPO":{"max-calls":20,"max-reserved-tokens":640000,"output-tokens":12000,"model":"gpt-6.1-terra","reasoning-effort":"low"},
  "QUERY_EMBEDDING":{"max-calls":250,"max-reserved-tokens":400000},
  "INDEX_EMBEDDING":{"max-calls":400,"max-reserved-tokens":40000000}},
 "query-embedding-model":"text-embedding-3-small"}}}
```

모델은 31화 Terra/Luna 비교 실험 결과로 정했다(계획 Terra medium, 판정 Luna high, 오탈자 Terra low). 판정 동시 4개는 Worker 쪽 값이다.
Luna 호출의 사용자 차감은 기존 `AiTokenChargePolicy`(Luna 1/20)를 그대로 따르고 Terra 1/2 차감은 후속 범위다.

Job configuration v2는 `processor.ValidationExecutionConfiguration` 한 곳에서만 만든다(접수 `queuePrepared`와 legacy 전환). 공통 fixture `configurationV2`와
`ValidationExecutionContractTest`가 같은 모양을 확인한다.

```json
{"configurationVersion":2,"leaseMillis":60000,"maxLeaseRecoveries":2,
 "budgets":{"setting":{"modelCalls":200,"lookups":300,"seconds":3600.0,"inputTokens":80000,"segmentChars":10000,"contextChars":1000,"searchLimit":10},
            "typo":{...같은 key...},
            "roles":{"PLANNER":{"maxCalls":10,"maxReservedTokens":800000,"outputTokens":50000},
                     "JUDGE":{...},"TYPO":{...},"QUERY_EMBEDDING":{"maxCalls":250,"maxReservedTokens":400000},
                     "INDEX_EMBEDDING":{"maxCalls":400,"maxReservedTokens":40000000}}},
 "budgetRevision":1,"budgetHistory":[],
 "models":{"PLANNER":{"model":"gpt-6.1-terra","reasoningEffort":"medium"},"JUDGE":{"model":"gpt-6-luna","reasoningEffort":"high"},"TYPO":{...},
           "QUERY_EMBEDDING":{"model":"text-embedding-3-small","version":"v1","dimensions":1536},
           "INDEX_EMBEDDING":{"model":"text-embedding-3-small","version":"v1","dimensions":1536}}}
```

claim은 `configurationVersion`·`models`·`budgets`를 그대로 싣는다. 모델·추론 강도는 코드 상수가 아니라 이 설정이 정한다(`ValidationRole`의 하드코딩 강도 삭제).
호출 하나의 예약은 `budgets[branch].inputTokens + roles[role].outputTokens` 이하다. count와 generation은 같은 모델·옵션·schema를 쓴다.
QUERY_EMBEDDING은 선택 사항이며 명시 모델 text-embedding-3-small과 별도 cap을 함께 설정할 때만 예약한다.
이는 기존 색인 계약의 모델 호환성이고 벡터 readiness나 검색 성공을 보장하지 않는다. 필요한 VECTOR가 미설정/미준비이면 정상 빈 검색으로 숨기지 않는다.

AiTokenUsage는 analysis/chat/validation 중 정확히 하나의 실제 owner를 가진다. validation 예약에는 원래 lease/generation을
보존하고 기존 AiTokenAccount/charge policy를 재사용한다. raw provider tokens와 계정 환산량을 혼동하지 않는다.
새 예약은 현재 lease·실제 잔액·역할 call cap·할당 raw cap과 call input/output 상한을 확인한다.
확정 실제 사용량이 예약량보다 크면 큰 값을 cap에 쓴다. 실패한 실제 호출도 소비하며, 확실히 provider를 호출하지 않은 RELEASE만 hold와 cap을 반환한다.
응답 유실·취소·만료·source 무효화는 UNKNOWN으로 hold를 풀되 사용량 0을 주장하지 않고 원래 정산 권한을 보존한다.
정산은 같은 측정값에만 멱등이며 다른 값이나 다른 실제 owner는 409다.

역할 cap은 **실행(run) 단위**다. `validation_jobs.run_start_generation`(V113, 기존 Job은 1)을 두고 작가의 명시 retry(`ValidationJob.retry()`)만
새 generation으로 옮긴다. 자동 lease 회수(`expire()`)는 바꾸지 않으므로 같은 실행으로 이어서 센다. cap 집계는 `validationGeneration ≥ runStartGeneration`이고
RELEASED가 아닌 사용량 행이다. 이전 실행의 늦은 SETTLE은 그 행의 generation이 바뀌지 않으므로 새 실행 cap에 들어가지 않는다.
Worker의 `InvocationBudget`(model-calls, lookups, seconds)은 claim 시도 단위이고, 회수를 넘는 실행 단위 권위는 서버 cap이다.
retry 전 cap 소진 사전 거절(`ensureValidationRetryCapacity`)과 query embedding 재사용 판정(`requiresNewQueryEmbedding`/`reusableVector`)은 삭제했다.
새 실행은 항상 처음부터 세며 RAG 검색 결과도 실행마다 새로 만든다.

retry의 예산 갱신(`ValidationExecutionBudgetPolicy.refreshForRetry`):
- configuration v2: `roles.*.{maxCalls,maxReservedTokens,outputTokens}`와 setting/typo의 `{modelCalls,lookups,seconds,inputTokens}`만 설정값으로 바꾼다.
  모델·`segmentChars`·`contextChars`·`searchLimit`은 접수 당시 값을 유지한다.
- legacy configuration: `budgets.setting`을 설정에서 새로 만들고, `budgets.typo`도 새로 만들되 저장된 typo `segmentChars`·`contextChars`는 유지한다
  (TYPO grid를 유지하고 `extendGrid`가 엄격 비교). PLANNER·JUDGE 모델은 설정에서, 저장된 TYPO 모델(과 추론 강도)은 유지한다. 퇴역 역할은 지운다.
  QUERY_EMBEDDING 최초 도입·일치 검사는 기존과 같다. `configurationVersion=2`로 바꾸고 `budgetHistory`에 `configurationVersion`·`modelsBefore`·`modelsAfter`를 남긴다.
  legacy 갱신은 바뀐 값이 없어 보여도 조기 반환하지 않는다.
budgetRevision과 budgetHistory(before/after/generation)를 기록하며 동일 retry key가 이를 다시 적용하지 않는다. 자동 증액은 없다.
미사용 Job의 optional embedding 최초 도입은 기존 실제 embedding 호출과 성공 VECTOR receipt가 모두 없을 때만 허용한다.

QUERY_EMBEDDING 예약 전에 adapter가 실제 query의 UTF-8 SHA-256(공백·대소문자·Unicode 정규화 없음), inputDigest/manifestHash,
기존 색인 model/version/dimensions를 typed queryInput으로 고정한다. 원장의 immutable validation_call_input은 V95의 nullable JSONB이고
일반 generation 역할은 null만 허용한다. 같은 requestId를 다른 queryInput으로 재전송하면 거절한다. 원문·query 본문은 원장에 저장하지 않는다.
성공 VECTOR receipt의 readiness.query_embedding은 원래 requestId와 동일 proof를 보존한다. 재사용은 한 실행 안의 같은 검색 문장에 한한다.
version은 기존 chat.retrieval-embedding-version override를 snapshot하고 차원은 기존 vector(1536)이다. 접수 때 색인 채우기(아래 절, INDEX_EMBEDDING) 밖에서 새 색인/백필·모델 교체나 자동 cap 증액은 하지 않는다.

`/plans`는 SETTING 계획(PLANNER, MERGED_PLAN) 전용이며 TYPO_SEGMENT는 `/units`의 제안·분류 검증을 거쳐야 한다.


## 접수 때 색인 채우기 (2026-11-02 결정)

결정 메모: "RAG 색인은 검수를 접수할 때 그 작품의 빈 곳(앞 회차·설정집)만 채운다. 검수를 안 쓰는 작품은 채우지 않는다." 그리고 "설정집도 분석할 때
회차처럼 RAG 색인에 올린다(조각 + 임베딩)". 이번 범위는 접수 때 채우기(회차·설정집)와 판정 VECTOR가 설정집 조각을 근거로 돌려주는 것이다.
분석 단계의 설정집 임베딩은 하지 않았다(회차도 분석 때는 검색 조각 준비만 하고 임베딩은 별도 경로다).

- **범위(RAG scope).** 고정 manifest의 앞 회차(`sourceKind=EPISODE`)와, 접수 때 그 설정집의 현재 판이고 보관되지 않은 revision만이다. 서버가 접수
  (`ValidationBaselineService.capture`)에서 SETTING_BOOK manifest 행마다 boolean `currentRevision`을 기록한다. 과거 판은 확정 Fact 출처 확인용으로만
  남고 색인·검색하지 않는다(회차도 현재 원고만 색인한다). 설정집 수정·보관은 기존처럼 그 report를 무효화하고, 그 설정집의 색인 행도 같은 트랜잭션에서
  지운다(`SettingBookRetrievalIndexCleanup`).
- **저장.** 회차는 기존 V76 `retrieval_sources`/`retrieval_chunks`, 설정집은 V114 `retrieval_setting_book_sources`(PK revision)/
  `retrieval_setting_book_chunks`(같은 모양, 조각 정책 `cl100k-800-100-boundary-v1`, 원문 slice와 Unicode code point offset). 작품 파기는 두 표도 지운다.
- **언제.** 검수 Job이 claim된 실행의 준비 단계다. Worker가 INPUT·T1·SUBJECTS를 읽은 직후 채우기를 시작해 PLANNER와 동시에 진행하고, JUDGE는
  채우기가 끝난 뒤(최대 `min(남은 seconds, 1200초)`) readiness를 한 번 읽고 시작한다. 접수 직후 Job이 claim되므로 사실상 접수 때 채운다.
- **빈 곳만.** 출처마다 header(key·version·hash·정책)가 고정 manifest와 같고 모든 조각이 그 색인 model/version의 유효한 READY 벡터이면 원문도 읽지 않고
  건너뛴다(벡터를 읽지 않는 집계 SQL 한 번). 아니면 data API READ_SOURCE(서버가 manifest·hash 확인)로 전문을 읽어 같은 규칙으로 자르고 기존
  `RetrievalStore.prepare`가 같은 조각 묶음은 그대로 두고 달라진 원고는 지운 뒤 다시 만든다(섞지 않음). READY가 아닌 조각만 32개씩 임베딩한다.
  이미 READY인 조각은 다시 임베딩하지 않으므로 retry·lease 회수·두 번째 실행은 남은 조각만 채우거나 아무것도 하지 않는다.
- **쓰기 연결.** 검수 Worker의 쓰기용 DB 연결은 명시 플래그 `--index-url-env`(운영 Compose `VALIDATION_INDEX_DATABASE_URL`, 기본 빈 값)로만 켠다.
  검색 색인 표만 쓰고 validation 상태는 쓰지 않는다. 비어 있으면 채우지 않고 지금처럼 판정한다. 읽기 전용 reader는 설정집 색인 표 두 개의 SELECT가
  더 필요하다(`app/retrieval/bootstrap.py` RELATIONS 12개, 관리 DSN 재실행으로 멱등 부여).
- **비용.** 역할 `INDEX_EMBEDDING`(원장 purpose `VALIDATION_INDEX_EMBEDDING`)이며 QUERY_EMBEDDING과 같은 색인 identity를 쓴다. 선택 역할이고 켜려면
  QUERY_EMBEDDING도 있어야 한다. 실행 단위 cap(`maxCalls`, `maxReservedTokens`)과 lease로 막고 원장에 raw 사용량을 남기며, 차감은 서비스 부담
  `SERVICE_PAID_V1`(작가 계정 hold·차감 0)이다. 기존 검색 색인 임베딩(OPERATIONAL_BACKFILL)이 작가 과금 경로를 쓰지 않는 규칙을 따른다.
  임베딩 역할은 분기 호출당 입력 상한(`inputTokens + outputTokens`) 대신 실행 cap으로만 막는다. 예약마다 typed `indexInput`
  (`contractVersion, inputDigest, manifestHash, sourceRef, sourceHash, chunkCount, chunkDigest, indexModel, indexVersion, indexDimensions`)을
  고정하며 sourceRef는 이 실행의 앞 회차 또는 `currentRevision` 설정집이어야 하고 sourceHash가 그 contentHash와 같아야 한다. `chunkDigest`는
  묶음 조각 `chunk_hash`를 줄바꿈으로 이은 SHA-256이다. 다른 역할은 indexInput을 보내지 않는다. 사용량 조회는 v1 key를 유지해 INDEX 행의 고정 입력도
  `queryInput` 칸에 보인다.
- **실패.** provider 오류·역할 cap·쓰기 DB 오류·원문 변경·읽을 수 없는 원문(예: 2,000,000 byte 초과 설정집)·시간 초과는 채우기만 끝낸다. 실행은 계속되고
  준비가 덜 된 색인은 준비됐다고 하지 않으므로(“범위 전부 임베딩됨” 규칙 유지) 검색이 필요한 질문만 INCOMPLETE `failure_code=VECTOR_INDEX_FILL_FAILED`,
  `failure_cause=INDEX_FILL_*`로 남는다. 쓰기 연결이나 역할이 없어 채울 수 없으면 지금처럼 `VECTOR_NOT_READY`다. lease 상실·입력 변경은 실행을 멈춘다.
  실패한 조각은 FAILED로 표시되며 다음 채우기가 MISSING과 같이 다시 임베딩한다.
- **설정집 근거.** VECTOR는 회차·설정집 조각을 같은 거리 순위로 합쳐 top-k(`searchLimit`)를 고른다(같은 거리면 회차 먼저). 설정집 조각은 그
  SETTING_BOOK 출처의 R ref·`source_kind=SETTING_BOOK`·`source_context{revisionNo, referenceTimeKind, referenceEpisodeNo}`인 E ref가 되고,
  판정 프롬프트는 SETTING_BOOK이 설정집이며 회차가 아니라고 적는다. 서버는 인용 검증을 그대로 적용한다: 고정 identity(hash·version)와 같고, 출처마다 한 번
  hash 확인한 설정집 전문을 code point로 잘라 quote와 비교한다.
- **배포.** Backend(V114, INDEX_EMBEDDING 역할·`currentRevision` manifest)와 AI를 같은 release로 올린다. api.env 예산 JSON에 INDEX_EMBEDDING을 넣지 않으면
  Worker는 채우지 않는다. 배포 전에 접수한 report의 manifest에는 `currentRevision`이 없어 설정집이 범위 밖이다(회차는 채운다).

## Python 실행기와 의미 재개

`EpisodeValidationWorker.claim_next/process_claimed/aclose`는 기존 호출형 inspect/check_typos와 별도다.
`ValidationExecutionClient`가 CommonResponse.data/실제 lease/scope를 사용하며, Python은 validation DB에 쓰지 않는다(접수 때 색인 채우기는 검색 색인 표만 쓴다).
claim의 configuration v2에서 PLANNER·JUDGE·TYPO `RoleRuntime(model, LlmCallOptions, max_output_tokens)`을 만들고 역할마다 metered client를 둔다.
PLANNER는 모든 구간을 병렬로 호출하고 각 단계를 검증 직후 게시한다. 모든 구간이 끝나면 서버 v4 grid의 segments를 로컬 분할과 비교한 뒤
MERGED_PLAN으로 서버 check UUID/revision/checkInputDigest를 받는다. SETTINGS를 질문 주체 전체에 대해 100개씩 미리 읽고(실행 안 캐시·중복 요청 합침)
JUDGE를 4개씩 동시에 실행한다(프로세스 공통 `LLM_MAX_CONCURRENT_REQUESTS` 안). 판정이 끝난 질문은 즉시 게시하고, 질문 단위 오류로 끝난 질문은
INCOMPLETE로 게시한다(위 ③). RAG 검색 전체(embedding·검색·proof 기록)는 실행당 한 lock으로 직렬화하고, 같은 문장은 기다렸다가 앞선 결과를 재사용한다.
조각마다 저장소를 다시 읽는 확인은 하지 않는다(인용 검증은 게시 때 서버가 한다).

재개는 저장된 PLANNER 단계와 MERGED_PLAN check, 결정된 unit을 그대로 쓴다. JUDGING runtime checkpoint는 없으므로 게시되지 않은 질문과 INCOMPLETE 질문은
첫 판정 호출부터 다시 한다. 오탈자는 ClassifiedTypoSuggestion의 SPELLING/SPACING/TYPO와 정확한 span을 보존하고 실패 구간만 실행한다.

두 분기는 독립 asyncio task이며 한 분기의 provider/role cap 실패나 질문 0개는 다른 분기를 취소하지 않는다.
typed role-cap/token quota 409는 분기 실패로 처리한다. lease/current input/source 공유 무효화와 전체 취소는
양쪽 children 및 새 예약/게시를 멈추되 원래 사용량의 UNKNOWN/늦은 실제 SETTLE는 유지한다.

### grid v4 배포 호환

projectionVersion·grid v4·configuration v2가 함께 바뀐다. 옛 Worker는 `projectionVersion: 2`를, 새 Worker는 `1`과 configuration v2가 아닌 claim을
거절하므로 섞인 배포는 바로 실패한다. QUEUED·RUNNING 검수 Job이 없을 때 api.env의 `SPRING_APPLICATION_JSON`을 먼저 새 설정으로 바꾸고(옛 설정의
RELATED_SELECTION·COVERAGE 키가 남으면 켜진 Backend가 기동하지 않는다) Backend(V113 포함)와 AI를 같은 release로 올린다.
배포 때 남아 있던 legacy 설정 Job을 새 Worker가 claim하면 `EXECUTION_BUDGET_CONFIGURATION_INVALID`로 시작하지 않고 lease 회수 한도 뒤 FAILED가 된다.
그 Job과 PARTIAL·FAILED legacy Job은 작가의 공개 retry가 SETTING 분기를 버리고 configuration v2·grid v4로 다시 만든다(TYPO 분기는 유지).

### GH349 grid v2 배포 호환 (legacy)

서버는 `prepare`마다 설정에서 units를 다시 계산하고 `extendGrid`가 저장된 grid 기준과 비교한다. 그래서 배포 전에 claim된
SETTING Job은 배포 뒤 `VALIDATION_SOURCE_VERSION_CONFLICT`로 끝나며 공개 retry(새 grid)로 다시 실행해야 한다.
배포는 RUNNING 검수 Job이 없을 때 Backend와 AI를 같은 release로 올린다. grid v1 서버에 새 Worker가 붙으면 첫 PLANNER 게시 뒤
`GRID_VERSION_UNSUPPORTED`로 SETTING 분기를 멈추고, 이미 저장된 v1 grid를 복원한 Job도 SUBJECT_RESOLUTION 전에 같은 코드로 멈춘다.
Backend는 기록 보존을 위해 `RELATED_SELECTION` stage enum을 유지하지만 새 grid가 요구하지 않는다. Worker는 저장된 v1 stage를 읽지 않는다.
grid v3(GH349 후속, 1글자 제외)는 Backend가 3을 만들고 저장된 SETTING grid가 3이 아니면 공개 retry에서 버리고 다시 만든다. Worker는 2·3을 모두 받으므로
새 Worker를 먼저 올려도 v2 서버와 동작하며(1글자 규칙만 서버 grid를 따름), 압축 투영은 서버 wire를 바꾸지 않는다.
bundle ref 행(GH349 후속 2)에는 순서가 있다. 새 서버는 옛 Worker의 전체 행 bundle도 받지만(추가 key 무시) 옛 서버는 bundle settings를 정본 행과 비교하므로
새 Worker의 DECIDED 게시를 거절한다. Backend를 먼저 또는 같은 release로 올리고, 검수 Job이 돌기 전에 둘 다 배포한다.

## 명시적 실행과 수동 검증

운영은 Worker Compose 서비스 `ai-validation-worker`(기본 0 replicas, `VALIDATION_WORKER_PROCESS_COUNT`)로
명시 opt-in한다(#344). 이 서비스는 `--enable`·`--poll-seconds`·`--backend-url`과 분석 Worker의 내부 키
(`SPRING_INTERNAL_API_KEY`)·provider key(`LLM_API_KEY`) 환경변수 이름을 명시해 연속 실행하며, S3·metrics·
tracing은 쓰지 않는다. 과거 원고 의미 검색 플래그(`--readonly-url-env VALIDATION_READONLY_DATABASE_URL
--query-embedding --index-version`)도 고정으로 주지만 기본 URL 값이 비어 있어 검색을 제안하지 않는다(GH344 후속, grid v4,
아래 끝 문단). 켜기·끄기는 AI `deploy/WORKER_EC2_DEPLOYMENT.md`를 따른다. 로컬 확인은 AI checkout에서 아래
전용 명령을 그대로 사용한다.

```sh
PYTHONPATH=. python scripts/run_validation_worker.py --enable --once \
  --backend-url "$VALIDATION_BACKEND_URL" --worker-id validation-explicit
```

INTERNAL_API_KEY/LLM_API_KEY는 기존 secret 환경으로 주입한다. 이 명령은 실제 provider를 호출할 수 있으므로
운영 검증은 별도 승인 절차에서 수행한다. --enable이 없거나 claim의 역할/분기 예산이 빠지면 시작하지 않는다.
연속 실행에는 --poll-seconds를 명시한다. .env를 읽거나 운영 환경을 자동 수정하지 않는다.
연속 실행은 Job 단위로 실패를 격리한다(GH340): 한 claim의 IncompleteInspection(EXECUTION_STOPPED 등)이나
예기치 않은 예외는 고정 코드/예외 종류와 jobId·reportId만 로그에 남기고 다음 claim을 계속하며, 연속 실패는
poll×2^n(최대 60초)만큼 기다렸다가 정상 반복 뒤 초기화한다. 취소와 종료 중인 Worker는 루프를 끝내고,
--once는 claim 하나만 처리하며 실패를 로그 뒤 다시 던져 0이 아닌 코드로 종료한다.
프로세스당 동시 실행(GH343): 연속 실행은 --concurrency개의 Job 슬롯을 둔다. 빈 슬롯을 먼저 확보한 뒤에만
claim하고 받은 Job은 즉시 별도 Task로 실행하며, claim한 Job을 프로세스 안에 쌓는 대기열은 두지 않는다.
claim 간격은 기존과 같다: claim 시도마다 poll 간격(연속 실패 시 위 backoff)을 한 번 쉬고, 슬롯이 모두 차
있으면 하나가 빌 때까지 기다린 뒤 쉰다. 그래서 --concurrency 1은 기존 순차 루프와 claim·처리 순서·로그가 같다.
빈 claim·claim 실패·한 Job의 IncompleteInspection/예외는 다음 claim만 늦추고 실행 중인 다른 Job을 취소하지
않는다. Job 단위 격리와 로그 형식(GH340)은 바뀌지 않는다. Backend claimValidationJob이 후보 Job 행을 잠그고
lease token을 새로 발급하므로 동시 claim에 Backend 변경은 필요 없다.
--llm-concurrency는 그 프로세스의 모든 Job·역할·query embedding provider 요청이 함께 쓰는 semaphore 하나다
(예약·provider 호출·정산을 감싼다. exact input count 요청은 분석 metering과 같이 이 상한 밖이다).
SIGTERM/SIGINT는 새 claim을 멈추고(진행 중인 claim 요청은 끝까지 받고 받은 Job도 실행한다) 실행 중 Job에
--shutdown-grace-seconds를 준 뒤 남은 Job을 취소한다. 취소한 Job은 실패로 보고하지 않으며 Backend lease 만료와
재claim이 복구한다(고정 로그 `validation runner shutdown grace expired pending_jobs=N` 한 줄). 기본값은 프로세스
환경변수에서만 읽는다: --concurrency는 AI_WORKER_CONCURRENCY(없으면 1), --llm-concurrency는
LLM_MAX_CONCURRENT_REQUESTS(없으면 상한 없음, 기존 동작), --shutdown-grace-seconds는
AI_WORKER_SHUTDOWN_GRACE_SECONDS(없으면 180초)이며 1 미만·0 이하는 시작 전에 거절한다.
--once는 동시성·종료 신호 옵션과 무관하게 claim 하나를 그 자리에서 처리한다.
검색은 --readonly-url-env에 SELECT 전용 DB URL 환경변수명을 주면 실제 frozen RAG adapter에 연결된다.
connection은 default_transaction_read_only=on이고 index model/version/1536 차원을 검증한다.
--query-embedding은 서버의 별도 cap/model과 기존 준비 index가 모두 있을 때만 사용한다.
미설정/미준비 vector는 정상 빈 검색으로 숨기지 않는다.
GH344 후속: 명시성은 플래그에 둔다. 플래그는 명시해야 하지만 그 환경변수 값은 비어 있을 수 있다.
--readonly-url-env의 변수가 없거나 비어 있으면 INFO `frozen retrieval disabled: <NAME> is empty` 한 줄을 남기고
frozen RAG 없이 실행한다(KeyError로 끝나지 않는다). 읽기 전용 session이 없는데 --query-embedding이면
INFO `query embedding disabled: no read-only retrieval sessions`를 남기고 embedding client 없이 실행한다
(ValueError 대신). --once도 같다. URL이 있으면 연결 풀은 --concurrency개(overflow 0)로 묶어 배포 연결 예산에
`프로세스 수 × 동시 Job 수`로 넣는다. 연결은 connect 5초·statement 5초(`statement_timeout`, readiness 5초 상한과 같은
근거)·idle-in-transaction 10초·풀 대기 5초로 DB/드라이버가 끊는다. asyncio timeout은 `asyncio.to_thread`의 블로킹 조회와
그 연결을 멈추지 못하기 때문이며, 끊긴 조회는 readiness면 검색 미제안, 검색이면 기존 `SEARCH_FAILED`로 끝난다. Job마다 claim에 QUERY_EMBEDDING model/cap이 아예 없으면 embedding 없이
검색을 제안하지 않고(있는데 index와 다르거나 cap이 0이면 `EMBEDDING_CONFIGURATION_INVALID`), 회차 출처가 있으면
Job 시작 때 고정 범위 readiness를 한 번 읽어 모든 회차 출처의 vector가 준비됐을 때만 VECTOR를 제안한다
(INFO `vector search offered=<bool> sources=<n> ready=<m> ...`). 이 읽기는 `min(남은 seconds, 5초)`로 제한하며,
실패·초과하면 예외 종류만 경고하고 그 Job은 frozen RAG 없이 실행한다(첫 PLANNER 예산을 쓰지 않는다). grid v4에는 키워드 검색이 없으므로
검색이 없으면 변화 검색이 필요한 질문은 `VECTOR_NOT_READY`로 끝난다.
검색 계정은 챗봇 계정과 같고 `validation_reports`·`validation_jobs`·`validation_baseline_sources` SELECT가 더 필요하다.
챗봇 bootstrap(`app.retrieval.bootstrap`)이 관리 계정에 이 권한을 부여·갱신한다.
grid v4에는 JUDGING runtime checkpoint가 없어 재개 때 VECTOR를 유지하는 예외도 없다. 게시되지 않은 질문은 그 시점의 readiness로 다시 판정한다.

합성 검증은 loopback gh310_execution_test 이외의 DB에서 실행하지 않는다. 실제 Spring HTTP·JPA·Flyway와
새 Python Worker를 연결하는 ValidationExecutionHttpPostgresIntegrationTest는
GH310_EXECUTION_POSTGRES_JDBC_URL/GH310_AI_CHECKOUT/GH310_PYTHON을 명시한다.
provider와 S3만 합성이고, 이 검증을 실제 모델 의미 품질이나 운영 배포 결과로 해석하지 않는다.
저장 계획/정상 판정 crash 뒤 provider 추가 호출0, 영향 revision만 재판정, 첫 책 성공 보존,
정상0건/PARTIAL0finding/FAILED 구분, query proof 재사용/거절, 변경·파기·만료 후 게시0와 실제 정산을 확인한다.
모든 상태에서 추출 Job/후보/자동 확정·철회는 0이다. 작가 판단·추출 release #312, UI #314는 별도다.
