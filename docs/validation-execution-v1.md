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
| `POST /{jobId}/revisions` | Revise: 영향 check 전부의 CAS·새 lookup_obligations·변경 사유. 전체 성공 또는 전체 rollback |
| `POST /{jobId}/checkpoint` | Checkpoint: branch, 그 분기의 expectedVersion, 의미 payload/citations |
| `POST /{jobId}/complete` | Complete: Scope, key, nullable 안전한 failureCode. 서버가 저장 처리 범위로 상태 결정 |
| `POST /{jobId}/usage/reserve` | Reserve: 실제 call requestId, role/model/raw 예약량 |
| `POST /{jobId}/usage/{requestId}` | Usage: SETTLE/UNKNOWN/RELEASE, 원래 예약 Scope와 lease |

표의 `/{jobId}` 접두사는 `/api/internal/v1/validation-jobs`다.
공개 `GET /api/v1/works/{workId}/episode-validations/{reportId}/results`는 상태·처리 수·coverage 요약,
현재 check 요약·finding·검증된 인용만 반환한다. 실행 중/중단 후 모두 읽기 전용이다.
내부 전체 evidence_bundle/catalogue/checkpoint/lease/호출 원장 상세는 공개 응답에 내보내지 않는다.
`POST /.../{reportId}/retries`는 expectedReportVersion과 idempotencyKey로 같은 입력을 다시 queue한다.

## 초기 호출과 서버 발급 모델 입력

초기 순서는 순환하지 않는다.

1. claim에서 서버 Scope/회원·작품·report revision, baseline/target/manifest hash, 실제 lease/attempt,
   projectionVersion=1/clueVersion=1, 고정 모델과 명시 budgets를 받는다.
2. claim.checkpoint 또는 GET checkpoint로 이미 저장된 단계/check를 먼저 복원한다. 초기 requiredGrid=null은 정상이다.
3. 기존 data 경로의 INPUT과 ENTRIES를 읽는다. 전용 Job이 있으면 기존 회원 헤더에
   `X-Validation-Job-Id`와 실제 lease를 반드시 추가한다. Job 없는 기존 callable만 기존 읽기 계약을 유지한다.
4. INPUT의 `modelSourceCatalogue`와 ENTRIES의 `modelSettings`를 그대로 사용한다.
   `projectionVersion`을 검증하며 S-ref는 전체 고정 baseline 순서에서 발급되어 페이지에 따라 달라지지 않는다.
   `authorCorrectionProofs`는 해당 entryRef에 연결된 원래 plural typed 정정 사실의 신뢰 서버 전송이다.
5. 실제 T1 전문 READ_SOURCE의 `modelClues`와 clueVersion=1을 사용한다. CL은 서버가 발급한 code point
   위치/인용이며 각 segment core에 start가 속하는 CL만 COVERAGE에 제공한다. 실행 경로가 Python의
   로컬 `_settings`/`collect_clues`로 조용히 fallback하면 안 된다. Job 없는 기존 callable만 fallback을 보존할 수 있다.
6. 각 검증된 의미 단계를 즉시 저장하고 MERGED_PLAN 영수증의 안정 check ID/revision/digest를 저장한 뒤 JUDGE를 호출한다.

projection은 기존 값·이력·조건·시점을 보존하고 서버 ID/hash/key/token metadata만 모델 투영에서 제거한다.
원문 provenance가 정확히 고정 manifest와 일치할 때만 R-ref를 추가한다. canonical author_correction은 null이다.
정정 기록은 실제로 존재하지만 질문의 자유문자열 서사 시점/조건 권위로 추론하지 않는다.
이 계약에서 DB_RAW_DISAGREEMENT는 NEEDS_CONTEXT이며 #312의 작가 판단으로 검토할 수 있다.
정확한 typed 시간·조건 대응이 입증되는 후속 계약 전에는 자동 NO_CONFLICT 승격이나 광범위한 예외를 만들지 않는다.

CL v1은 기존 Python 세 정규식과 반복 이름의 의미를 보존한다. 숫자는 명시적 ASCII `[0-9]`, 공백은 Python Unicode
`\s`(U+001C..001F 포함), 위치는 code point다. `(start,end,kind)`를 정렬·중복 제거한 뒤 CL 순번을 준다.
이름/별칭은 literal로 두 번 이상 나타날 때만 힌트가 된다. 힌트 자체는 finding이나 새 사실이 아니다.

## 의미 단계와 완료 증명

서버가 실제 전문 길이와 frozen entry count, 설정집 길이, 고정 segment/context/page 크기로 필수 grid를 계산한다.
Worker가 required-grid 또는 done:true를 보내 완료를 대체할 수 없다.

- PLANNER: target segment별 QuestionBatch.
- RELATED_SELECTION: target segment × 전체 catalogue page별 RelatedSelection.entries.
- COVERAGE: target segment별 QuestionBatch. 선정한 모든 rule과 정확한 서버 CL 집합의 CHECKED/NOT_APPLICABLE 기록을 요구한다.
- BOOK_SOURCE: 설정집 전문을 읽었다는 source_read_status=READY와 실제 planned_checks 기록. 빈 책도 읽기 성공+0 checks를 저장한다.
- BOOK_COVERAGE: 각 target segment × 각 book segment의 QuestionBatch/rule 처리.
- MERGED_PLAN: 저장된 모든 원 질문 originalRefs를 빠짐없이 연결한 질문 목록. 서버 UUID와 local Q-ref의 대응을 반환한다.
- TYPO_SEGMENT: 분류(SPELLING/SPACING/TYPO)와 정확한 제안 또는 제안 0개의 완료 segment.

SETTING과 TYPO grid는 독립 준비한다. SETTING의 gridVersion=1은 frozen manifest의 모든 BOOK_SOURCE 의무를
먼저 넣고, 전문을 아직 확인하지 못한 book은 `books=[{sourceRef:...}]`와 `pendingBookRefs`로 남긴다.
미확정 book은 빈 책/0 checks가 아니다. PLANNER/RELATED_SELECTION/COVERAGE 및 checkpoint는 다른 book 전문을
읽지 않고 저장한다. 성공 BOOK_SOURCE 게시가 그 book만 실제 전문 검증하여 totalCodePoints/plannedChecks/segments와
해당 BOOK_COVERAGE 의무를 확정한다. 같은 Work 아래 최신 grid에 단조 병합하므로 동시 준비가 서로를 덮지 않는다.
이미 성공한 stage/receipt와 checkpoint를 회수 후 복원하고 실패한 book만 다시 준비한다.

pendingBookRefs가 하나라도 있으면 MERGED_PLAN·SETTING 완료·NO_CHECK 성공은 불가하고 전체 gridReady=false다.
모든 book 길이가 확정되면 다른 target 구간이 미완료여도 그 구간의 모든 필수 처리가 끝난 NO_CHECK 성공은 유지한다.
어떤 book이 일시적으로 읽히지 않아도 target의 성공 단계와 TYPO는 독립 저장할 수 있다.
실제 source 변경/파기/불일치는 모든 frozen source metadata의 최종 재검증·전체 무효화로 거절하며 일시 실패와 섞지 않는다.
target 전문 실패나 빈 target은 어느 쪽도 정상 완료가 아니다.
Python의 BOOK_SOURCE.status=PROCESSED는 전체 book 검토 집계이므로 실제 전문 읽기와 혼동하지 않는다.
Java의 BOOK_SOURCE 성공은 읽기이며 전체 target×book 검토는 별도 BOOK_COVERAGE grid가 증명한다.

각 DECIDED unit에는 현재 Python ValidationDecision과 `evidence_bundle`을 함께 보낸다.
bundle의 question_ref/check_revision/question은 저장 질문과 일치해야 한다. canonical frozen settings,
실제 E-ref/원문 위치, target_context와 additional_target_context의 인용, 알려진 fact/evidence refs,
성공한 실제 비교·필수 읽기 receipts, 남은 lookup_obligations 0을 검증한다. 네 completion 사유만으로 완료하지 않는다.
정상 NO_CONFLICT도 완료 check/receipts를 저장한다. E-ref는 실제 source_ref로 분류한다. T1을 E1로 포장해도
CONFLICT/SUSPECTED_CONFLICT의 외부 비교 근거가 될 수 없고 frozen fact 또는 검증된 T1 이외의 source만 인정한다.
T1 자체 문맥·인용 저장은 유지한다.
새 FOLLOW_UP은 영향을 받는 모든 check를 한 트랜잭션에서 revision 변경하고 옛 완료를 집계에서 제외한다.
원 상태/판정과 changeReason은 check history에 남겨 #312가 변경된 질문을 구분할 수 있다.

종료 상태는 현재 revision의 완료 check(NO_CONFLICT 포함), 완료 typo segment(제안 0개 포함),
필수 PLANNER/selection/COVERAGE/book/CL이 모두 저장되고 미해결 질문이 없는 NO_CHECK segment로 결정한다.
계획/선정/원문 읽기만 성공한 것은 성공 결과 수에 넣지 않는다.
성공 처리 0+미완료는 FAILED, 일부 성공+필수 미완료는 PARTIAL, 양쪽 모든 grid와 현재 질문이 완료되어야 COMPLETED다.
어느 경우에도 추출 Job·후보·자동 확정은 0이며 작가 판단 API를 추가하지 않는다.

## 게시·파기·재시도

게시 외피 전체는 131,072 UTF-8 bytes, citations는 1,000개, 의미 payload 깊이는 50을 넘기지 않는다.
target_spans/spans/evidence 행/typo span과 target_context/additional_target_context라는 계약 경로의 quote/text는
정확한 source/hash/version/code point 인용 외피가 필요하다. settings[]는 서버 canonical projection 전체와 정확히
일치해야 하며 그 내부 도메인 속성 이름 text/quote를 span으로 해석하지 않는다. 다른 임의 payload/checkpoint 경로의
text/quote는 거절하므로 검증되지 않은 복사본문을 추가 필드에 숨길 수 없다. canonical 설정도 깊이 상한을 지킨다.
I/O 전에 현재 lease/입력을 확인하고 실제 저장 원문 전체 hash·slice를 확인한 뒤, Work 잠금 안에서
current/revision/contentRevision/lease/출처 metadata를 다시 검사한다. S3/provider I/O 동안 Work를 잠그지 않는다.
같은 report/key의 exact payload는 원 receipt를 반환하고 다른 payload는 409다. 이미 바뀐 check revision의 unit은
기존 key 재전송이라도 거절한다. 성공한 단계의 새 key exact 전송은 상태나 공개 version을 다시 바꾸지 않는다.

같은 입력 retry는 report/성공 stages/check ID를 보존하고 미완료만 새 generation으로 실행한다.
공개 새 retry는 최신 활성 회차만 허용한다. 원래 성공한 retry key는 새 행동이 아니므로 원 summary receipt를 반환한다.
유효 saved seed의 준비 중단은 원래 hash/fingerprint·target/source 수명을 검증하고 기존 materialize로 복구한다.
seedless/invalidated/purged 입력을 그 경로로 복원하지 않고 Task 1의 명시적 새 baseline 접수를 사용한다.

ReportInvalidated는 baseline과 같은 트랜잭션에서 stage/check/history/finding/evidence/checkpoint/publication 및 grid의
복사본문을 삭제하고 실제 Job을 취소한다. 원장은 원래 소유권을 유지해 늦은 실제 정산을 받을 수 있다.
Work/회원 파기는 RESERVED/UNKNOWN 호출도 기존 worker drain에 포함하고 계정 hold를 해제한 뒤 FK 자식을 먼저 지운다.
물리 파기 완료 뒤에는 기존 Job/원장도 없으며 그 이후 정산 소유권을 새 Job에 옮기지 않는다.

## 실행 설정과 사용량

`validation.execution.enabled`는 기본 false다. 켜려면 lease-duration, max-lease-recoveries,
setting/typo의 model-calls/lookups/seconds/input-tokens/output-tokens/segment-chars/context-chars/catalogue-page-size/search-limit와
다섯 역할 roles.*.max-calls/max-reserved-tokens를 모두 명시한다. production 수치 기본값을 만들지 않는다.
정확한 Map enum 키를 보존하는 외부 YAML 또는 SPRING_APPLICATION_JSON을 사용할 수 있다.
테스트 수치는 `src/test/resources/validation-execution-fixture.properties`이며 운영 권장값이 아니다.
비활성 앱은 예산 없이 정상 기동하며 과거 report를 시작 시 일괄 queue하지 않는다. 활성화 뒤 기존 waiting report는
명시 START/RECHECK의 동일 접수 복구 또는 공개 retry로 시작한다.

모든 generation 역할은 Terra이며 PLANNER medium / RELATED_SELECTION high / COVERAGE high / JUDGE medium / TYPO low다.
RELATED_SELECTION은 독립 보완의 실제 transport 역할이고 BOOK_COVERAGE는 COVERAGE 역할을 사용한다.
count와 generation의 동일 옵션/schema는 Task3 adapter가 기존 검사기에서 유지해야 한다.
QUERY_EMBEDDING은 선택 사항이며 명시 모델 text-embedding-3-small과 별도 cap을 함께 설정할 때만 예약한다.
이는 기존 색인 계약의 모델 호환성이고 벡터 readiness나 검색 성공을 보장하지 않는다.
필요한 VECTOR가 미설정/미준비이면 정상 빈 검색으로 숨기지 않는다.

AiTokenUsage는 analysis/chat/validation 중 정확히 하나의 실제 owner를 가진다. validation 예약에는 원래 lease/generation을
보존하고 기존 AiTokenAccount/charge policy를 재사용한다. raw provider tokens와 계정 환산량을 혼동하지 않는다.
새 예약은 현재 lease·실제 잔액·역할 call cap·할당 raw cap과 call input/output 상한을 확인한다.
cap은 Job 전체의 RESERVED/UNKNOWN/SETTLED 누계이며 확정 실제 사용량이 예약량보다 크면 큰 값을 사용한다.
실패한 실제 호출도 소비하며, 확실히 provider를 호출하지 않은 RELEASE만 hold와 cap을 반환한다.
응답 유실·취소·만료·source 무효화는 UNKNOWN으로 hold를 풀되 사용량 0을 주장하지 않고 cap 및 원래 정산 권한을 보존한다.
정산은 같은 측정값에만 멱등이며 다른 값이나 다른 실제 owner는 409다.

cap이 소진된 알려진 필수 미완료 generation 역할은 같은 설정 retry를 409로 거절한다. 운영자가 명시 설정을 변경한 뒤
사용자가 retry하면 Job 전체 누계를 유지한 채 새 cap-기존 사용량으로 재개한다. 자동 증액은 없다.
이때만 modelCalls/lookups/seconds/input/output 한도를 채택하고 segment/context/catalogue/search 폭·모델/추론은 보존한다.
budgetRevision과 budgetHistory(before/after/generation)를 기록하며 동일 retry key가 이를 다시 적용하지 않는다.
미사용 Job의 optional embedding 최초 도입은 기존 실제 embedding 호출과 성공 VECTOR receipt가 모두 없을 때만 허용한다.
이미 사용한 모델을 조용히 교체하지 않는다. 기존 모델/검색 정책을 바꾸는 별도 운영 조치는 이 작업에 포함되지 않는다.


QUERY_EMBEDDING의 retry preflight는 실제 원 호출 identity와 typed semantic checkpoint를 함께 확인한다.
예약 전에 adapter가 실제 query의 UTF-8 SHA-256(공백·대소문자·Unicode 정규화 없음), inputDigest/manifestHash,
기존 색인 model/version/dimensions를 typed queryInput으로 고정한다. 원장의 immutable
validation_call_input은 V95의 nullable JSONB이고 일반 generation 역할은 null만 허용한다.
같은 requestId를 다른 queryInput으로 재전송하면 거절한다. 원문·query 본문은 원장에 저장하지 않는다.
성공 VECTOR receipt의 readiness.query_embedding은 원래 requestId와 동일 proof를 보존한다.
서버는 현재 질문의 실제 VECTOR 의무·원래 조회 당시 SUCCEEDED/완전 READY·원래 SETTLED SUCCESS
ValidationJob 사용량과 proof 전체를 대조한다. 새 embedding이 필요하고 cap이 소진됐으면 queue 전에 409,
호환 성공 receipt와 JUDGE만 남으면 cap 소진이어도 retry를 허용하며 추가 embedding은 0이다.
원래 receipt가 failed/partial/not-ready이거나 다른 질의·입력·manifest·고정 model/version/차원이면 재사용하지 않는다.
이미 성공한 조회의 JUDGE-only 재개는 live index 재구축/FAILED/embedding-null 때문에 막지 않는다.
저장된 근거와 원 호출 proof를 사용하고 현재 input/source/hash는 기존 leased 경계에서 다시 검증한다.
version은 기존 chat.retrieval-embedding-version override를 snapshot하고 차원은 기존 vector(1536)이다.
새 색인/백필·모델 교체나 자동 cap 증액은 하지 않는다.

`/plans`는 SETTING 계획 전용이며 TYPO_SEGMENT는 `/units`의 제안·분류 검증을 거쳐야 한다.


## Python 실행기와 의미 재개

`EpisodeValidationWorker.claim_next/process_claimed/aclose`는 기존 호출형 inspect/check_typos와 별도다.
`ValidationExecutionClient`가 CommonResponse.data/실제 lease/scope를 사용하며, Python은 validation DB에 쓰지 않는다.
PLANNER/전체 RELATED_SELECTION page/COVERAGE/각 BOOK_SOURCE/각 BOOK_COVERAGE를 검증 직후 게시한다.
MERGED_PLAN은 첫 JUDGE 전에 서버 check UUID/revision/checkInputDigest를 발급받고 정상 NO_CONFLICT도 즉시 저장한다.
FOLLOW_UP은 영향 질문 전부를 원자 revise한 다음 새 판단을 실행한다. 같은 입력의 checkpoint는 version만 전진하고
원래 receipt와 근거를 보존한다. revision 저장 직후 crash는 서버 현재 의무와 이전 runtime revision 차이로 복원한다.
source/report 파기와 입력 교체는 이 보존 경로를 쓰지 않고 기존 lifecycle에서 본문 전체를 지운다.

checkpoint v1은 성공 stage/decision을 중복 복사하지 않는다. 전역 receipts와 질문별 refs, 남은 queue,
필요한 실제 근거/추가 문맥/현재 관측 revision만 보존한다. evidence는 phase별 한 곳에 둔다.
성공 BOOK_SOURCE와 서버 segments로 준비 상태를 복원하고, 미완료 rule은 인증된 READ_SOURCE 범위로 읽는다.
질문이 없는 큰 책 전체를 checkpoint로 복사하지 않는다. 131072-byte 상한과 인용 검증은 그대로다.
오탈자는 ClassifiedTypoSuggestion의 SPELLING/SPACING/TYPO와 정확한 span을 보존하고 실패 구간만 실행한다.

두 분기는 독립 asyncio task이며 한 분기의 provider/role cap 실패나 질문 0개는 다른 분기를 취소하지 않는다.
typed role-cap/token quota 409는 분기 실패로 처리한다. lease/current input/source 공유 무효화와 전체 취소는
양쪽 children 및 새 예약/게시를 멈추되 원래 사용량의 UNKNOWN/늦은 실제 SETTLE는 유지한다.

## 명시적 실행과 수동 검증

기본 runner는 추가하지 않는다. AI checkout에서 명시 opt-in으로만 아래 전용 명령을 사용한다.

```sh
PYTHONPATH=. python scripts/run_validation_worker.py --enable --once \
  --backend-url "$VALIDATION_BACKEND_URL" --worker-id validation-explicit
```

INTERNAL_API_KEY/LLM_API_KEY는 기존 secret 환경으로 주입한다. 이 명령은 실제 provider를 호출할 수 있으므로
운영 검증은 별도 승인 절차에서 수행한다. --enable이 없거나 claim의 역할/분기 예산이 빠지면 시작하지 않는다.
연속 실행에는 --poll-seconds를 명시한다. .env를 읽거나 운영 환경을 자동 수정하지 않는다.
검색은 --readonly-url-env에 SELECT 전용 DB URL 환경변수명을 주면 실제 frozen RAG adapter에 연결된다.
connection은 default_transaction_read_only=on이고 index model/version/1536 차원을 검증한다.
--query-embedding은 서버의 별도 cap/model과 기존 준비 index가 모두 있을 때만 사용한다.
미설정/미준비 vector는 정상 빈 검색으로 숨기지 않는다.

합성 검증은 loopback gh310_execution_test 이외의 DB에서 실행하지 않는다. 실제 Spring HTTP·JPA·Flyway와
새 Python Worker를 연결하는 ValidationExecutionHttpPostgresIntegrationTest는
GH310_EXECUTION_POSTGRES_JDBC_URL/GH310_AI_CHECKOUT/GH310_PYTHON을 명시한다.
provider와 S3만 합성이고, 이 검증을 실제 모델 의미 품질이나 운영 배포 결과로 해석하지 않는다.
저장 계획/정상 판정 crash 뒤 provider 추가 호출0, 영향 revision만 재판정, 첫 책 성공 보존,
정상0건/PARTIAL0finding/FAILED 구분, query proof 재사용/거절, 변경·파기·만료 후 게시0와 실제 정산을 확인한다.
모든 상태에서 추출 Job/후보/자동 확정·철회는 0이다. 작가 판단·추출 release #312, UI #314는 별도다.
