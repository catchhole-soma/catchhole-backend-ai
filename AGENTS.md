# Repository Guidelines

## Pull Requests

- 챗봇 검토·사용 중 새로 확인한 조회/답변/비용 한계는 [Java #247](https://github.com/catchhole-soma/catchhole-backend-java/issues/247)에 재현 조건·현재/기대 동작·검증 상태·관련 PR을 누적한다. 미검증 우려와 확인된 문제를 구분하고, 개별 개선 PR로 누적 이슈 전체를 자동 종료하지 않는다.
- PR을 작성할 때 `.github/pull_request_template.md`의 섹션과 체크리스트를 유지하고 실제 변경에 맞게 모두 채운다.
- 관련 Jira 이슈와 GitHub 이슈·PR을 본문에 연결하고, 리뷰어가 재현할 수 있는 검증 명령과 결과를 참고 사항에 기록한다.
- `main` 대상 PR은 `.github/workflows/test.yml`에서 전체 pytest를 실행한다. DB를 사용하지 않는 단위 테스트는 로컬 `.env`나 CI의 `DATABASE_URL`에 의존하지 않고 경계 의존성을 주입·mock한다.
- 운영 이미지 발행과 Worker 배포는 `main` push에서 시작된 `Publish AI Image` 성공 흐름으로만 실행한다. Worker 배포는 해당 publish run의 commit SHA가 현재 `main`일 때만 진행하고, 그 SHA로 Compose와 이미지 태그를 함께 고정하며, Backend `main` 최신 커밋의 API 배포 성공과 Spring health를 확인한 뒤 시작한다.

## AI Logic Version Records

- 결과에 영향을 주는 추출·주체 해소·비교·후처리·프롬프트·제품 모델/실행 설정 변경 PR마다 `docs/ai-logic-versions/vNNNN.md`(사람용 요약), `docs/ai-logic-versions/details/vNNNN.md`(AI용 상세), 목록을 함께 갱신한다. 작성 절차와 근거 항목은 `docs/ai-logic-versions/details/README.md`를 따른다.
- 사람용 요약은 변경 이유·점수 표·남은 문제를 중심으로 20~35줄 안팎으로 쓴다. 전체 SHA·해시·실행 옵션·과거 평가·반복 지침은 상세에 둔다. 상세를 먼저 기록하고 요약의 수치·조건·상태를 맞춘다.
- 기본 품질 평가는 다단계 `FIXED` 모드에서 추출 `gpt-5.6-sol`·주체 해소 `gpt-5.6-terra`·비교 `gpt-5.6-sol`, 제품 `LLM_REASONING_EFFORT=medium`으로 실행한다. `LLM_MODEL` fallback은 `gpt-5.6-terra`, judge는 Sol·medium이다. 로컬 CLI에도 명시하고 실제 사용값을 기록한다.
- 상세에는 복원할 전체 SHA, 구현 PR, 실제 코드·채점기 SHA, 입력 식별값, 모델·judge 조건, 집계 점수·분모·미판정/실패 수를 남긴다. 머지 전 번호 충돌을 확인하고 squash/rebase 후 최종 복원 SHA를 보완한다. 과거 실행은 보존하고 `null`을 0으로 바꾸거나 다른 조건의 점수를 개선폭으로 표시하지 않는다.
- 미측정·실패·부분 완료는 사유·담당자·재평가 계획을 기록하면 머지를 허용한다. 오류·미판정과 필요한 다음 조치는 요약에도 남긴다. 원문·정답·개별 예측·비밀값은 요약과 상세 모두에 복사하지 않는다.
- 문서·테스트만의 변경은 버전을 올리지 않는다. 채점기·Gold·judge만 바뀌면 같은 로직의 상세에 실행을 추가하고 요약을 갱신한다. 예전 로직 복원도 최신 main에서 새 PR과 다음 버전으로 남긴다.

## Spring Worker API

- 기본 `analysis` Worker는 `supportsFrozenComparisonEvidence=true`를 claim에 전송한다. `ORDERED_PROVISIONAL` 응답의 `comparisonEvidenceContract.formatVersion`이 정수 1인지 progress·heartbeat·원문/저장·provider 호출 전에 확인하고, 누락·잘못된 타입·미지원 버전은 기존 lease 기반 fail로 보고한다. 구 `CONFIRMED_ONLY`와 두 hidden 재비교 Worker의 광고는 유지한다.

- 설정집 출처는 명시적인 `sourceKind=SETTING_BOOK`과 `settingBook`의 고정 리비전으로 선택한다. 회차 null 여부로 출처를 추측하거나 회차 번호·첫 등장 회차를 만들지 않는다. 세 Worker 모두 `supportedSourceKinds=[EPISODE,SETTING_BOOK]`과 `supportsSettingBookReferenceTime=true`를 광고한다. 책 종류 지원만 광고한 구 Worker는 기준시점 입력을 claim하지 않는다.
- 설정집 추출은 `AUTOMATIC + ORDERED_PROVISIONAL`의 같은 인물/세계관 단계를 사용한다. `setting_book_chunks`는 Job·리비전으로 격리하고 임베딩을 생성하지 않는다. 직접 청크/인물 후보 저장과 체크포인트 재개에는 작품·배치·활성 파일·현재 리비전·해시/key/version·lease·입력 hash·청크 소유권을 검증하며 저장된 청크/후보 ID를 유지한다. 출처 변경·보관·A→B→A 리비전 전환으로 이전 lease를 되살리지 않는다.
- 설정집 비교는 common `source`와 책 출처 provenance를 유지한다. prompt에는 문서명/리비전 번호만 투영하고 내부 UUID/hash를 넣지 않는다. 표·제목·목록의 사실을 읽되 리비전을 이야기 시점이나 영구적인 출처 우선순위로 해석하지 않는다. 숨김 재비교 Job의 `CONFIRMED_ONLY`도 고정 책 출처를 검증한다. 사용자 거절 fingerprint·자동 반영·journal seal은 Spring이 소유한다.
- 책의 `referenceTimeKind=UNSPECIFIED|WORK_START|EPISODE`와 EPISODE의 양수 `referenceEpisodeNo`를 불변 Job/리비전으로 검증한다. raw UTF-8 SHA는 그대로 두고 기준시점은 별도 input hash에 포함한다. WORK_START를 회차 0으로 만들지 않는다. 자동 경로의 양립하는 목적지 MERGE는 기존 의미와 출처를 보존해 알려진 같은/이전 기준시점도 허용한다. 명시적 정상 UPDATE·STATUS 종료는 모든 알려진 `currentSourceTimes`보다 뒤여야 하며 PRESENT BOOK의 선택적 UNSPECIFIED 기준시점 공백만 허용한다. 이동·출처 제거와 수동 경로는 기존 엄격한 경계를 유지한다. 시점·출처 불명, 미래·과거·가정·본문/기준 anchor 상충과 값 차이만의 변경은 검토하며 시점·회차를 만들지 않는다. PRESENT EXCLUDE와 읽기 전용 인물 검토 참조도 본문/기준 anchor 상충을 검사한다.
- BOOK은 서버가 준 `currentValueProtected`, `protectedSlots`, 대상별 `protectedPaths`를 목적지·matched·removed·root move 전체에 적용한다. 없는 protected slot도 복원하지 않는다. 단건/묶음/복구/후속 projection에서 표식을 유지하고 MERGE는 기존 CURRENT 출처 전체+새 출처, UPDATE는 새 출처만 유지한다. `reviewSource=HUMAN` 자체를 직접 수정 표식으로 추측하지 않는다. 실제 저장·명시적 현재 반영·권한·버전/출처 재검증은 Spring이 소유한다.
- 동명 발견을 이름만으로 하나의 provisional anchor로 합치지 않는다. 같은 frozen source와 동일 located evidence 반복만 dedupe하고, 여러 앵커/기존 동일 fullname은 typed resolver 근거로 연결하거나 review에 남긴다. 정당한 short/full-name·별칭 연결을 유지한다. 서로 다른 offset/anchor 자체를 별개 인물 증명이나 서버 자동 승격 허가로 취급하지 않는다.
- GH236의 `SettingToolsClient`는 분석 Worker와 독립적인 읽기 전용 설정 조회 client다. `SettingToolContext`의 회원·작품은 신뢰 서버가 인증된 실행에서 고정하며 모델 인자로 받지 않는다. 현재값·이력·원문 상태는 Spring 계약을 보존하고 빈 결과로 축약하지 않는다. 계약과 Java/Python fixture 동기화는 `docs/setting-tools-v1.md`를 따른다. 챗봇 실행에 AnalysisJob/Worker lease를 임의 생성하지 않는다.

- 분석 runner는 claim의 `allowedJobTypes`를 명시한다. 기본 `analysis` 프로세스는 `SETTING_EXTRACTION`, 별도 `character-comparison`/`world-comparison` 프로세스는 각각 사용자 재비교용 `CHARACTER_FACT_COMPARISON`/`WORLD_SETTING_COMPARISON`만 claim해 서로의 작업을 가져가지 않는다. `character-comparison` Worker만 `supportsCharacterComparisonGroups=true`를 보내 새 입력 hash 그룹 Job을 claim한다.
- claim 뒤 상태 변경, checkpoint, 세계관 후보 API와 토큰 예약에는 `X-Worker-Lease-Token`을 전송하고, 장기 provider 호출 중에는 60초 주기로 heartbeat를 보낸다. lease가 만료된 응답을 우회해 DB 상태를 직접 바꾸지 않는다.
- `SETTING_EXTRACTION`의 재시작 경계는 `CHUNKS_READY → CHARACTER_CANDIDATES_SAVED → CHARACTER_COMPARISONS_HANDED_OFF → legacy CHARACTER_COMPARISONS_FINISHED → WORLD_CANDIDATES_PUBLISHED → WORLD_COMPARISONS_FINISHED` 순서를 사용한다. 새 원 Job은 후보 게시만 그룹 숨김 Job에 인계하고 직접 캐릭터 비교를 실행하지 않는다. legacy finished를 예약 완료로 재해석하거나 완료된 stage의 외부 호출·저장을 반복하지 않는다.
- 캐릭터 `setting_candidates` 저장은 기존 SQLAlchemy 경계를 유지한다. 세계관 `world_setting_candidates` 생성·비교 상태 저장은 반드시 Spring 내부 Worker API를 사용하며, Python이 `world_settings`나 세계관 후보 테이블을 직접 수정하지 않는다.
- Python은 `setting_candidates.comparison_status`의 최초 값만 저장한다. 후보 claim 이후 상태 전이, snapshot context/version 검증, 비교 결과 저장, `CharacterFact` append와 `WorkCharacter` snapshot 반영은 Spring이 소유하며 Python은 공유 DB를 직접 갱신하지 않는다.
- 캐릭터 batch 비교 prompt에는 Backend UUID 대신 source 후보 `C*`, 시작 snapshot `P*`, 후보가 만들 projected slot `Q*` 요청 로컬 참조만 제공한다. exact/alias와 비-STATUS pattern key는 고정하고 pattern STATUS만 의미가 같은 canonical key로 정규화한다. complete에는 resolved key와 `P*`/`Q*` target·removal, projector가 계산한 `C*` dependency, `contextToken`을 보내며 실제 ID는 노출하지 않는다.
- batch PRESENT REVIEW_REQUIRED의 target은 같은 canonical Fact 유형·key·조건의 명백한 비양립 항목을 읽기 전용으로 연결할 때만 허용한다. proposal·removal은 비우고 target의 실제 현재 묶음 기여자만 dependency로 보낸다. 일반 불확실성·결과 없는 치료 시도·비PRESENT·본문/BOOK anchor 상충의 검토는 target=null이다. REVIEW는 투영하지 않으며 구 단건 API의 mutation target 의미를 바꾸지 않는다.
- 캐릭터 batch claim에는 `X-Character-Comparison-Person-Batches: true`를 광고한다. 새 캐릭터 batch는 Spring이 고정한 동일 인물의 모든 Fact 유형 후보를 원문 순서로 처리한다. 구 Backend가 반환하는 유형별 payload도 계속 처리한다. batch/context의 `canonicalFactType=null`이면 각 candidate의 `canonicalFactType`을 반드시 사용하며, 타입이 있는 예전 저장 batch는 기존 유형 범위를 유지한다. 새 인물 batch는 모든 유형의 snapshot·보호 슬롯·이력을 전달하고 30개 문맥 상한을 적용하지 않는다. CONFIRMED_ONLY는 같은 논리 그룹과 input hash의 후보를 회차 순서로 비교하며 신규 그룹은 빈 snapshot/version 0에서 시작한다. ORDERED_PROVISIONAL은 회차별 고정 입력으로 원 Job 안에서 비교를 끝내고 다음 회차 전에 journal과 자동 저장을 완료한다. 각 source 후보는 정확히 한 decision 또는 typed failure로 덮고 서로 병합하지 않으며, 앞선 성공 decision만 메모리 projected snapshot에 적용한다. schema 재시도 뒤 batch 검증이 실패하면 legacy는 원문 순서 singleton fallback을 하고, ordered는 마지막 응답에서 독립적으로 재검증한 정상 판단을 유지하며 실패·의존 후보만 원문 순서로 한 번씩 재비교한다. quota는 fallback하지 않는다. stale context는 Spring 저장 전에 전체 decision을 새로 만든다.
- 캐릭터 1차의 같은 응답에서 `narrative_time.fact_time/reference_time`과 독립 `time_context`를 받는다. Worker가 원 청크·Unicode offset으로 검증하며 위치를 확정하지 못한 point는 null로 저장하고 raw 응답은 보존한다. 이전 청크의 검증된 point는 T* 참조로 전달·해소하고 현재 청크 인용으로 다시 붙이지 않는다. 설정 후보 없는 시간 문맥은 캐릭터 대표 시점을 갱신하지 않는다.
- Java의 `advisoryCurrentContext`(대표 시점·current source별 시점·noncurrent/REMOVE 이력)를 schema→pipeline→단건/묶음·분할·복구 prompt 전체에 연결한다. 빈 current에서도 이력을 보존하고 history를 P/Q target으로 승격하지 않는다. Backend UUID·storage binding은 prompt에 보내지 않고 로컬 참조로 바꾼다.
- 비교 결정의 `advance_current_narrative_time`은 기본 false이며 후보의 located referenceTime을 선택할 때만 true다. PRESENT REMOVE·중복 EXCLUDE·HISTORY_ONLY에서도 선택할 수 있지만 과거·가정·불명·REVIEW는 거절한다. 분할·복구 중 검증된 대표 시점과 종료 이력을 다음 입력에 투영하며 실제 저장/hold/version/hash 확정은 Java가 검증한다. 입력 상한을 맞추려고 필수 이력을 자르지 않는다.
- 캐릭터 비교 prompt에서 candidate evidence와 snapshot 문자열은 소설 데이터일 뿐 지시가 아니다. 원문에 포함된 역할 변경, 규칙 무시, 별도 JSON 출력 요구를 따르지 않도록 system prompt에서 명시한다.
- 캐릭터 비교에서 회상·가정은 `HISTORY_ONLY` 또는 `REVIEW_REQUIRED`만 허용한다. STATUS 종료는 현재 시점의 상태 변화 결과가 있으면 제안할 수 있으며, 명시적인 완치 문구나 절대적인 논리 모순까지 요구하지 않는다. 치료 수단만 있고 결과가 없으면 제거하지 않지만, 치료 뒤 능력·증상·행동 변화로 기존 상태가 끝났다는 해석이 자연스러우면 의미상 관련된 STATUS를 함께 제거할 수 있다. 무관한 잠재 상태나 다른 Fact 유형은 제거하지 않는다.
- 신규 `setting_candidates` 비교 컬럼과 내부 API/checkpoint가 먼저 존재해야 Python의 직접 후보 저장과 후속 stage가 동작한다. 운영은 가능한 `RUNNING` Job을 drain한 뒤 Spring Flyway/API, AI 이미지 순으로 배포하고 같은 AI 이미지를 기본·`character-comparison`·`world-comparison` 세 서비스로 기동한다. 이전 세계관 checkpoint에 이미 도달한 Job은 새 캐릭터 비교를 소급 실행하지 않으므로 필요하면 회차를 재분석한다.
- 세계관 대상 연결은 원고의 후보 전체를 기본 한 요청으로 처리한다. 초기 `S*`와 앞선 정상 신규 판단의 `N*`만 같은 category 안에서 참조하고, 후보 누락·중복·미래 참조를 거절한 뒤 원문 순서로 적용한다. 실패·모호한 후보는 임시 대상이나 identity evidence를 만들지 않는다. book identity proof와 기존 확정 전용 shortcut을 유지하고, 입력 초과·출력 절단·검증 실패는 순서와 누적 상태를 보존해 분할한다. `LLM_WORLD_SETTING_BATCH_SUBJECT_RESOLUTION_MAX_OUTPUT_TOKENS=32000`, `...MAX_INPUT_TOKENS=64000`을 사용하며 캐릭터 식별의 기존 2000 한도와 분리한다.
- 세계관 비교 prompt에는 Backend UUID를 노출하지 않는다. Worker가 만든 `S*`/`T*` 및 ordered 기존 속성 `T*.P*` 참조만 LLM에 제공하고, 실제 대상 ID·현재 property·version 검증과 `beforeValue` 산출은 Spring이 담당한다.
- 세계관 source 하나와 실제 현재 속성의 명백한 비양립은 CONFLICT + REVIEW_REQUIRED + GENERAL_UNCERTAINTY reviewReason으로 구분한다. 실제 target/matched/proposed canonical 경로·같은 원본 scope, 비어 있지 않은 모두 PRESENT인 currentSourceTimes, 확정 PRESENT 원문, 이동 없음만 허용하며 현재값과 새 원문값·근거를 함께 보존한다. 일반 SINGLE/GENERAL_UNCERTAINTY는 source 하나씩 비투영 검토로 유지하며 현재 항목의 소급 보류 근거로 사용하지 않는다.
- 세계관 묶음 비교는 후보별 canonical 주체를 먼저 해소해 Spring에 원자 저장한 뒤 시작한다. Spring이 `analysis job + source episode + category + canonical subject key + normalized raw scope`가 같은 후보만 한 batch로 claim하며, Worker는 claim된 batch를 원문 이름으로 다시 묶거나 나누지 않는다.
- canonical 주체 해소에서 정규화한 이름의 exact 대상은 최대 20개까지 모두 Spring에 보내 `AMBIGUOUS` 판정을 맡기고, LLM이 고르는 fuzzy 후보만 최대 3개로 유지한다. exact 대상이 20개를 넘으면 DTO 생성 전에 명시적 비교 검증 오류로 중단하며 앞의 일부만 잘라 보내지 않는다.
- 한 세계관 batch는 독립 속성별로 여러 decision을 반환한다. 모든 원본 C*는 검증된 decision 또는 typed failure에 정확히 한 번 포함한다. 병합 decision의 모든 source는 비교 복구·검수·확정에서도 하나의 단위이며, 다른 항목의 실패 때문에 원본 경로별로 다시 쪼개지 않는다.
- 세계관 내용 비교는 같은 고정 원고의 독립 batch를 AI 요청 하나로 묶되 Spring의 batch·source membership·저장 순서는 유지한다. 입력은 `LLM_WORLD_SETTING_BATCH_COMPARISON_MAX_INPUT_TOKENS`(기본 64,000), 예상 출력은 기존 출력 상한(16,000) 안에서 묶으며 공통 지시·schema·추론 여유를 포함한다. 같은 대상 재등장 또는 출처/시점 지시 차이는 요청 경계이며 원문을 잘라 맞추지 않는다. 응답의 G*/C*/T*는 그룹별로 검증한다. 각 저장 전에 전체 context가 원래 입력과 같은지 확인하고, subject reset 뒤에는 새 작업을 앞에서 다시 claim하며 이전 묶음 결과를 폐기한다. 실패 그룹만 기존 복구를 적용하고 수동 첫 실패·quota/lease/고정 입력 중단 정책을 보존한다. FIXED 평가도 같은 분할·복구 경계를 사용한다.
- 세계관 batch의 독립 decision은 source가 하나여도 신규 `ADD`라면 2차 LLM이 제안한 canonical `proposed_scope_name`·`proposed_setting_name`을 보존한다. 단, raw와 다른 새 scope는 현재 ADD, 기존 scoped property, 또는 `existing_root_property_names_to_move`로 함께 옮길 실제 root property를 합쳐 서로 다른 최종 하위 속성이 둘 이상일 때만 허용한다. 범위명과 설정명은 같을 수 없다. 독립 decision끼리 같은 상위 scope를 공유해도 source를 한 decision으로 합치지 않으며, 기존 단건 비교의 raw path 보정 규칙을 batch decision에 적용하지 않는다.
- batch context stale은 batch 전체 비교를 다시 만들고, canonical 주체 해소가 stale이면 기존 batch를 닫은 뒤 주체 해소와 새 batch claim부터 제한 횟수 안에서 다시 수행한다. quota·lease 만료·oversized batch는 부분 decision을 남기지 않는다.
- 세계관 `comparisonReason`은 검토 화면에 그대로 노출되는 사용자 문장이다. `S*`/`T*` 참조, UUID, key, version, operation enum 같은 내부 용어를 저장하지 않으며, 모델이 대상 참조를 반환하면 실제 대상명을 사용한 자연스러운 한국어로 치환한다. 실제 target의 주체·범위·설정명이 내부 token과 같은 영문 단어라면 표시명으로 쓴 부분만 허용하고, 그 외의 내부 enum·key 노출은 계속 거절한다.
- 기존 속성과 의미가 같아 세계관 후보를 `EXCLUDE`할 때는 2차 비교 결과에 해당 `target_ref`와 실제 `matched_property_name`을 함께 반환한다. Backend가 비교 당시 기존값을 `beforeValue`로 보존해야 하며, 일시적 사건처럼 특정 기존 속성과 비교하지 않은 제외만 두 값을 비울 수 있다.
- 세계관 비교는 같은 category와 canonical 주체의 전체 경로·값·원문 근거로 같은 사실인지 판단한다. 단순 정리용 scope/name 차이는 EXCLUDE/MERGE/UPDATE를 막지 않으며 빈 scope만으로 검토로 바꾸지 않는다. 같은 이름의 속성이 scoped 경로에만 있어도 모델이 실제 target/matched 경로를 명시적으로 선택한 정상 판단을 자동 검토로 바꾸지 않으며, root ADD도 기존 경로와 충돌하지 않으면 유지한다. 시점·현재값/보호 경로·확정성·source coverage와 실제 경로 검증은 계속 적용한다. 실제 지역·시점·조건·하위 집단·확실성 차이는 보존한다. 모델은 실제 기존 matched 경로를 선택하고 UPDATE/MERGE는 그 저장 경로를 유지한다. 원본 후보·근거는 바꾸지 않으며 EXCLUDE는 기존 상세 값을 변경하지 않는다.
- 분석 progress 요청은 표시용 `currentStep`과 대상 회차에 적용할 `episodeStatus`를 함께 보낸다. 자유 형식 문구에서 상태를 추론하지 않도록 `EpisodeProcessingStatus` enum을 명시적으로 직렬화한다.
- claim payload는 복수 `episodes`가 아니라 단일 `episode`를 받는다. 한 `AnalysisJob`은 한 회차만 처리하고, 회차 사이의 반복과 실패 격리는 Spring의 Job queue가 담당한다.
- 장기 실행 runner는 한 Job의 실패를 Spring에 보고한 뒤 다음 claim을 계속한다. 개별 분석 예외로 Worker 프로세스 전체를 종료하지 않는다.
- Spring Worker HTTP 거절의 예외·로그·실패 저장 문자열은 실제 HTTP status, 검토된 고정 error code, 기존 reason-code allowlist와 알려진 비교 DTO field 경로 최대 8개만 포함한다. 응답의 message·details.message·거절값·원시 body·내부 URL은 복사하지 않는다. 현재 서버가 constraint code를 주지 않으므로 문장에서 추정하지 않는다. 알려지지 않은 코드는 표시 요약에서 생략하되 원래 typed code·request/response 객체는 메모리에 유지하며, 기존 lease/stale/quota 처리와 Spring 400의 작업 중단 분류를 바꾸지 않는다. HTTP 응답 본문을 원시 예외 cause로 로그에 출력하지 않는다.
- Spring token reserve의 HTTP 409는 응답 `error.code`가 `AI_TOKEN_QUOTA_EXHAUSTED`일 때 전용 비재시도 예외로 바꾼다. 이 예외를 만난 후보를 typed failure로 보고한 뒤 같은 Job의 다음 후보를 claim하지 않으며, 다른 실행 중 Job Task는 취소하지 않는다.
- `source_chunk_id`는 LLM 생성값이 아니라 Worker가 가진 `EpisodeChunk.id`를 source of truth로 사용한다. LLM 응답에 값이 없거나 잘못되어도 Pydantic 검증 전에 현재 chunk ID로 덮어쓴다.
- 설정 추출 prompt에는 claim의 `knownCharacters` 대표 이름만 전달하고 Backend 내부 매칭용 `characterId`는 노출하지 않는다. 원문에 명시된 미등록 이름은 `candidate_kind=CHARACTER_DISCOVERY`로 추출하고 설정 payload는 모두 `null`로 두며, 기존 이름과 매칭되는 발견 후보와 같은 분석 안의 중복 발견은 저장 전에 제외한다.
- `knownCharacters[].activeStatuses`는 회차 시작 전에 활성인 STATUS의 `factKey`와 nullable `factValue`만 포함하고 임의 절단하지 않는다. 1차 prompt에는 상위 대표 이름을 `characterName`으로 결합한 최소 문맥만 전달하며 UUID·value JSON·provenance·history는 노출하지 않는다. 기존 상태의 단순 반복은 재추출하지 않고, 치료 수단만으로 종료를 단정하지 않으며 실제 기능·증상·행동 변화의 근거만 후보로 남긴다. 같은 회차 projected 상태 누적은 이 목록의 책임이 아니다.
- 캐릭터 1차 추출은 순간 감각·장면 전환 반응만으로 STATUS의 시작·종료를 만들거나 당연한 행동 정지를 별도 능력 제약으로 확대하지 않는다. 기존 활성 상태의 변경·종료와 사망·부활은 우선 보존하며, 같은 회차 안에서 끝난다는 사실 자체를 제외 기준으로 사용하지 않는다.
- STATUS 후보의 `value_json.active`는 존재하면 JSON boolean만 허용한다. candidate나 2차 proposal의 `active=false`는 현재 snapshot에 ADD/UPDATE/MERGE하지 않고 REMOVE 또는 비반영 판단으로 처리한다.
- 회차 시작 `activeStatuses`에는 기존 snapshot의 active 원본 값을 전달하지 않는다. Spring이 현재 slot으로 선택한 factKey와 nullable factValue를 문맥으로 신뢰하며, 신규 후보·제안의 active 타입 검증을 legacy snapshot 값에 소급 적용하지 않는다.
- `CHARACTER_DISCOVERY`의 캐릭터 매칭은 `entity_name`만 기준으로 한다. `케닉의 넷째 아들 세룸` 같은 `raw_entity_mention` 안의 기존 관계자 이름을 발견 대상 캐릭터로 오연결하거나 subject fallback으로 재해석하지 않는다.
- `SETTING` 후보는 회차·원문 버전, 확정 캐릭터 ID 또는 정규화한 구체 이름, `attribute_name`, `value_type`, canonical `value_json`, 유일하게 확인된 전체 evidence 위치가 모두 같을 때만 저장 전에 중복 제거하고 더 높은 confidence의 근거를 남긴다. 위치가 다르거나 반복 quote·null/불명확 evidence·`AMBIGUOUS` 주체인 후보는 변화·다른 사건 가능성이 있으므로 유지한다.
- `SettingCandidate.value_json`은 `JSONB(none_as_null=True)`로 매핑한다. `CHARACTER_DISCOVERY`의 Python `None`은 JSON literal `null`이 아니라 DB check constraint가 요구하는 SQL `NULL`로 저장해야 한다.
- 캐릭터 비교의 canonical `REMOVE`는 `target_ref=null`, `removed_snapshot_refs` 1개 이상, proposal 없음으로 출력한다. candidate와 같은 key 또는 다른 key의 의미상 관련된 현재 STATUS를 요청 로컬 `P*` 참조로 하나 이상 끝낼 수 있지만 non-STATUS·unknown ref·비현재 후보는 거절한다. 기존 `REMOVE + targetRef` 하위 호환 정규화는 먼저 배포되는 Spring이 담당하며 Python은 신규 형식만 생성한다.
- `NUMBER`/`BOOLEAN` 후보는 Pydantic 경계에서 `value_json.value`의 JSON 타입을 검증하고 Mapper가 저장 `attribute_value`를 그 값의 canonical 표현(NUMBER 숫자 문자열, BOOLEAN 소문자 `true`/`false`)으로 만든다. LLM이 보낸 원래 표시 문구는 Mapper 변환 전 payload로 `raw_ai_result_json`에 보존하고, 비교 proposal도 Spring에 보내기 전 같은 canonical 규칙을 적용한다. 표시값과 snapshot 대표값이 다른 상태를 새로 저장하지 않기 위함이다.
- 캐릭터 단건·batch 비교의 `STRING` proposal은 `proposed_value_json.value`가 JSON 문자열인지 응답 재시도 경계에서 검증한다. 잘못된 값을 임의로 문자열로 바꾸지 않으며, 재시도 소진 시 기존 후보별 typed failure로 처리해 평가 최종 결과 생성까지 오류를 넘기지 않는다.

## Async Worker Runtime

- `ORDERED_PROVISIONAL`은 claim의 명시적 `analysisMode`와 고정 `analysisContext`가 있을 때만 사용한다. Worker는 `supportedAnalysisModes`로 지원을 광고하고 누적 모드를 파일 수·남은 Job 수·임시 데이터 존재 여부로 추론하지 않는다. `CONFIRMED_ONLY`의 기존 모델·입력 의미와 구버전 응답 기본값을 유지한다. 최신 main의 공통 prompt를 기준으로 자연어 이유·세계관 주체 연결·일반 검토 지침만 더한다. main #65의 세계관 추출·분류 기준을 유지한다. user payload·schema·모델 계약은 유지하며 과거 80d 기준과 main 05d9c2d 기준은 별도 golden으로 보존한다.
- 누적 상태의 실제 ID와 `provisional-character:*`/`provisional-world:*` 식별자는 별도 필드로 전달한다. 임시 UUID를 실제 FK로 저장하지 않는다. ordered 캐릭터 연결은 추출된 이름과 등록된 이름·별칭의 완전/유일한 포함 관계를 코드로 검사한다. 복수 대상, 원문 표현과 추출 이름의 충돌, 같은 짧은 등록 이름에 걸리는 경쟁 신규 전체 이름은 보류한다. 모호한 결과를 적용된 현재 상태로 취급하지 않는다.
- ordered 캐릭터 인물 연결 LLM은 미연결 SETTING에 대해 현재 원문에 포함되지 않는 앞뒤 청크가 실제로 추가되고 선택 가능한 대상이 있을 때만 호출한다. 단일 청크의 미상, 빈/중복 문맥, 미상 발견 후보에는 호출하지 않는다. 이미 규칙으로 연결한 후보는 재판단하지 않으며 모든 발견의 원래 이름·근거와 신규 인물의 첫 발견 anchor를 보존한다. 자동 반영은 미연결 후보를 검토 대상으로 남기고 나머지를 처리한다. `CONFIRMED_ONLY`의 기존 fallback 정책은 유지한다.
- ordered 캐릭터·세계관 주체 해소는 명시적인 JSON 출력 예시와 strict `LlmResponseSchema`를 함께 전달한다. 응답의 모든 필드는 필수이며 캐릭터 대상 미해결은 생략 대신 명시적 `null`, 세계관 미해결은 대상 빈 목록과 명시적 모호성으로 표현한다. schema 검증 재시도는 최초 입력에 허용된 필드 경로·오류 종류만 덧붙이고 실패 응답·입력값·동적 key·UUID를 피드백에 복사하지 않는다. 필수 입력 상한에는 schema와 재시도 피드백도 포함하며, 반복 실패를 정상 미해결 판단으로 바꾸지 않는다. 기존 확정 설정 기반 호출은 선택 schema 인자가 없을 때 요청 형식을 유지한다.
- ordered 세계관 주체 해소에서 schema·대상 존재·중복 검증을 통과한 응답이 `ambiguous=true`와 유효한 선택 참조를 함께 주면 명시적인 불확실성을 우선해 참조를 비우고 해당 후보를 보류한다. Spring의 모호성 우선 계약과 같으며 추가 호출·강제 연결·신규 주체 생성·선택 대상에 근거 누적을 하지 않는다. 알 수 없는 참조, 중복 참조, schema 및 실행 실패는 이 처리로 숨기지 않는다.
- BOOK의 비선택 `identity_proofs`는 공급된 ref의 `DISTINCT|RELATED` 설명과 candidate 또는 해당 target의 연속 원문만 허용하며, 모호성 반환 전에도 검증하고 선택 대상·모호성·근거 누적에 사용하지 않는다.
- ordered 캐릭터 후보·청크·임베딩 직접 DB 저장은 같은 저장 트랜잭션에서 Job와 Episode를 짧게 잠근 뒤 lease·실행·입력 상태·원문 snapshot·checkpoint·journal 상태를 검증한다. 잠금은 LLM/S3 호출 동안 유지하지 않는다. 늦은 Worker가 현재 후보나 청크를 삭제하는 경로를 허용하지 않는다.
- ordered의 캐릭터·세계관 batch claim/context는 Job의 동일 `analysisContext`를 사용한다. 세계관도 `contextToken`을 완료 요청에 돌려주며 사용자 미확정 여부·출처 회차는 항목별로 모델에 제공하되 Backend 식별자는 제공하지 않는다. 필수 문맥을 임의로 자르지 않고 실제 입력 기준 한도를 검사한다.
- ordered 세계관 batch 비교는 기존 도메인 validator를 유지한 strict 응답 schema를 사용하며 nullable 필드도 생략 대신 명시적 null을 요구한다. 미확정 속성도 이미 존재하는 전체 경로로 비교하고, 기존 exact 경로의 ADD와 root 값·동명 scope 구조의 충돌을 거절한다. 재시도 피드백은 고정 오류 코드와 허용된 필드·판단 index만 사용하며, 경로 충돌은 원래 입력에서 다시 확인한 C*/T* 참조와 기존 property index·경로만 전달한다. 원시 예외·실패 응답값·임의 key를 복사하지 않고 Pydantic 도메인 오류는 정확한 고정 문구 allowlist에 일치할 때만 규칙 코드를 제공한다. 실패를 자동 UPDATE/EXCLUDE/REVIEW_REQUIRED로 바꾸지 않으며 기존 확정 설정 기반 비교는 main 기반 GH180 공통 prompt/cache를 사용하고 기존 schema 생략·입력 계약을 유지한다.
- ordered 세계관 batch의 단일 연결 대상은 임시 등록되었거나 속성이 비어 있어도 모든 decision이 그 `target_ref`를 유지한다. 대상 자체의 등록 상태를 입력에 표시하고 누락 시 `CANONICAL_TARGET_REQUIRED`와 원래 입력에서 검증한 참조로 재시도하며 Worker가 값을 자동 보정하지 않는다. ordered 세계관 batch의 최종 검증 요약은 고정 오류 코드·허용 필드·판단 index를 보존하되 원문·응답값·UUID·임의 예외 문자열은 포함하지 않는다.
- ordered 세계관 provider 응답은 기존 위치를 `matched_property_ref` 하나(예: `T1.P1`)로 선택한다. 입력의 `targets.properties[].ref` 전체를 strict schema의 선택 enum으로 제공하고 응답 ref와 target_ref가 같은 입력 대상에 속하는지 다시 검증한다. `matched_scope_name`/`matched_property_name`은 provider 응답에서 금지하며 입력 경로로 복원한다. UPDATE/MERGE의 proposed scope/name도 명시적 null만 허용하고 같은 선택 경로로 복원한다. ADD/EXCLUDE/REVIEW_REQUIRED는 실제 proposed 이름을 반환하며 Spring complete DTO에는 기존 full-path 필드를 유지한다. `CONFIRMED_ONLY`는 main 기반 GH180 공통 prompt/cache와 기존 schema·단건 계약을 사용한다.
- 범위가 없는 단일 후보와 이름이 다른 실제 scoped 속성의 관계가 불분명하면 단건·일반 batch·ordered에서 명시적 REVIEW_REQUIRED + SCOPE_UNRESOLVED를 허용한다. proposed scope는 null, 이름·값은 원본, 이동 목록은 []이며 실제 matched 경로를 보존한다. 검토를 자동 반영하거나 유효한 concrete 판단을 scope 누락만으로 검토로 바꾸지 않는다.
- 세계관 단건·batch의 `REVIEW_REQUIRED + GENERAL_UNCERTAINTY`는 범위 외의 대상·내용 불확실성을 모델이 명시적으로 판단한 정상 검토다. batch는 source 하나씩, 이동 목록=[]이며 최종 proposed 경로·값을 원본 후보로 복원한다. 선택한 target/property는 실제 입력에 존재해야 하고 ordered canonical target 유지·coverage 검증을 우회하지 않는다. 기존 scope 사유의 조건을 완화하거나 잘못된 응답·과거 실패를 일반 검토로 자동 변환하지 않는다. 일반 검토는 확정 설정이나 확정된 주체 연결이 아니며 Spring은 원본 주체·경로·값을 미확정 참고로 보존한다. 실패 후보의 재분석과 이미 확정된 주체 과병합 수정은 별도 작업이다.
- ordered의 명시적 범위 검토 사유는 원본 scope 유무로 구분한다. 원본 null에는 SCOPE_UNRESOLVED, 명시적 다른 scope에는 SCOPE_MISMATCH를 사용하고 원본 proposed 경로를 보존한다. 단순 분류 차이만으로 정상 의미 비교를 거절하거나 잘못된 응답을 검토로 변환하지 않는다.
- 세계관 공통 복구는 최초 응답 1회 뒤 각 decision을 schema·도메인·실제 경로로 독립 재검증한다. 정상 판단을 유지하고 실패·source 중복·읽기/쓰기 경로·root 이동·합성 scope로 연결된 판단만 묶어 한 번 재비교한다. 파싱 불가 응답은 전체를 한 번 재시도한다. 합산 coverage·경로·형제 조건을 마지막에 재검증하고 새 충돌은 해당 의존 단위를 실패로 남긴다. quota·lease·고정 입력·영구 provider·Spring 오류를 부분 성공으로 바꾸지 않는다.
- 복구 의존 관계 분석에서 대상 참조가 누락되고 요청 대상이 하나면 그 대상의 경로와 보수적으로 연결한다. 이 추론으로 원본 응답을 보정하거나 승인하지 않으며 재시도의 실제 target 선택·경로 검증은 유지한다.
- 명시적 범위 검토는 원본 source별 판단과 근거를 보존한다. 정상 병합 판단은 복구 중에도 모든 source와 canonical 경로를 함께 유지하며, 다른 항목 실패를 이유로 경로 정리를 취소하지 않는다.
- ordered 캐릭터 batch에서 현재 canonical slot에 ADD하는 기존 검증 실패만 `CANONICAL_SLOT_ALREADY_EXISTS`로 식별한다. 재시도에는 원본 입력에서 재확인한 후보 C ref·현재 활성 P/앞선 Q ref·허용 key만 제공하고 값·근거·실패 응답은 넣지 않는다. 앞선 UPDATE가 P를 Q로 바꾸면 현재 Q를 안내하며, 이전 응답이 정규화한 STATUS key는 입력에도 있는 경우에만 피드백에 표시한다. 모델이 의미에 맞는 MERGE/UPDATE/EXCLUDE/REVIEW_REQUIRED를 명시적으로 선택해야 하며 자동 연산 변환이나 key 변경을 하지 않는다. 공유 projection validator와 다른 오류의 처리 경계는 유지하고 새 ordered 지침은 batch 입력 상한 계산에도 포함한다.
- ordered 세계관의 기존 고정 ValueError 검증은 진단 전용 `OrderedWorldRuleDiagnosticError`로 규칙과 실제 입력 후보를 보존한다. 최종 경로 중복·root/scope 충돌·잘못된 범위 검토·기존 이유 유출 검증 등의 조건을 완화하지 않는다. 진단의 실제 입력 후보 참조는 검증된 읽기·쓰기·구조 의존성과 함께 실패 범위를 결정하며 진단 문자열만으로 성공을 인정하지 않는다. 알 수 없는 오류의 원인은 추측하지 않으며 임의 예외 문자열·거절 값·원문을 기록하지 않는다.
- 세계관 진단의 선택 필드 `stage`는 `RESPONSE_SCHEMA|PROPERTY_SELECTION|DECISION_VALIDATION|SCOPE_PLAN|PROJECTED_SCOPE_PLAN`, `phase`는 `BATCH|RECOVERY`다. 마지막 전체 응답과 분리 복구에도 원래 시도 순서를 보존한다. 이 필드는 내부 진단이며 사용자 판단 이유에 출력하지 않는다. 이전 진단의 필드 생략/null과 기존 네 필드 계약은 계속 허용한다. 입력/lease/quota/provider 실행 오류를 응답 검증 오류로 바꾸지 않는다.
- ordered 응답 검증은 성공 raw 결과와 검증 소진 예외의 `validation_diagnostics`에 시도별 안전한 이력을 제공한다. `attempt_number`, 고정 `rule_code`, 입력에서 재검증한 `candidate_refs`, 실제 선택한 허용 ref의 `selected_properties`와 입력 제안 목록인 `allowed_matched_properties`를 구분한다. 경로 항목은 ref/target_ref/scope_name/setting_name만 포함하고 최대 20개 제안을 보존한다. 출처가 특정되지 않는 schema/파싱 오류는 candidate_refs/selected_properties를 비운다. Spring 진단에는 selected_properties만 대상 ID로 매핑하며 제안 목록을 모델이 선택한 경로처럼 저장하지 않는다. 원문·값·근거·실패 응답·임의 key·원시 예외·비밀값은 기록하지 않는다. 진단 callback 자체의 오류가 본 검증 실패를 가리지 않으며 고정 입력/실행 오류는 callback에 전달하지 않는다.
- ordered 대상의 `properties`가 비어 있으면 UPDATE/MERGE나 matched 경로가 있는 EXCLUDE는 불가능하다. 새 사실은 ADD하고 내용 자체를 제외할 근거가 있는 EXCLUDE만 matched_property_ref를 null로 둔다. 같은 배치의 다른 후보나 이번 응답의 ADD는 아직 기존 속성이 아니며 matched 대상으로 사용할 수 없다.
- ordered 세계관 batch의 `REVIEW_REQUIRED + SCOPE_MISMATCH`는 모델이 명시적으로 제안한 검토만 허용한다. source는 명시적 scope를 가진 후보 하나이고, 같은 canonical 주체의 실제 matched 속성이 존재하며 그 scope가 원본과 달라야 한다. matched root 속성의 scope는 null일 수 있다. proposed scope/name은 원본 후보 경로 그대로, 이동 목록은 빈 목록이며 원본 근거를 수정하지 않는다. 비교 이유에는 속성의 의미상 관련성과 확인할 범위 관계를 설명한다. 없는 경로·무관한 속성·다른 주체·일반 오류를 검토로 자동 전환하지 않으며 일반 비교의 의미 판단과 실제 저장 경로 검증은 유지한다. 이 검토는 현재 설정을 자동 반영하지 않는다.
- ordered 재시도는 없는 matched EXCLUDE 경로(`MATCHED_EXCLUDE_PATH_NOT_FOUND`), 없는 matched 속성(`MATCHED_PROPERTY_PATH_NOT_FOUND`), 새 범위의 하위 속성 부족(`GENERATED_SCOPE_REQUIRES_SIBLINGS`)를 구분한다. 고정 지침과 입력에서 재확인한 source 경로·target ref·기존 property index/경로만 피드백에 전달하며, 실패 응답의 제안 경로나 원시 예외를 복사하지 않는다. 동일 source coverage를 유지한 전체 응답을 다시 검증하며 반복 실패를 정상 검토로 바꾸지 않는다.
- ordered 캐릭터 부분 복구는 마지막 실패 응답의 후보 수·참조·순서가 원본과 정확히 같을 때만 개별 schema·도메인·projection 검증을 수행한다. 검증된 판단만 메모리에 유지하며, 파싱·coverage가 불명확하면 모두 재비교 대상이다. 실패 후보와 같은 고정 key의 후속 후보는 다시 판단하고, STATUS는 서로 다른 key도 정규화·종료에 영향을 줄 수 있어 실패 뒤 전체 suffix를 다시 판단한다. 앞선 실패 Q를 참조할 수 없으며 REMOVE 후 부재에 대한 dependency도 보존한다. 복구 호출은 후보별 `max_attempts_override=1`이고 최초부터 singleton인 요청에는 추가 복구를 붙이지 않는다. quota·lease·고정 입력·Spring·예상 밖 오류는 부분 완료로 바꾸지 않으며 stale에서는 기존 한도 안에서 전체 문맥과 판단을 다시 만든다.
- ordered 세계관 주체 연결은 같은 분류에서 이름과 근거가 같은 개념·개체를 가리킬 때, 모델이 입력의 임시 대상을 명시적으로 선택해 재사용할 수 있다. 예를 들어 일반적인 게임 캐릭터의 장비 지표와 전투 지표는 같은 주체의 다른 속성일 수 있다. 이름만으로 강제 병합하지 않고 모호하거나 다른 실체이면 새 주체·보류를 유지한다. 관련된 분류나 상위·하위 종류는 동일 대상의 별칭이 아니다. 원문이 변이종·상위종·희귀종·상위 변이종을 따로 설명하면 구분하며, 다른 이름도 동일 대상의 별칭·약칭·번역이라는 근거가 있으면 명시적으로 연결할 수 있다. 추가 호출·다른 이름의 일괄 보류·강제 이름 병합은 하지 않는다. 명시적으로 선택한 한 대상에는 원본 후보의 근거만 메모리상 누적해 후속 연결에 제공하며, 원본 후보와 첫 신규 anchor는 바꾸지 않는다. 이전 회차 보류 참고는 선택 대상이나 확정 사실로 승격하지 않는다.
- 새 인물·세계관 비교 이유는 기존 정보와 새 근거의 관계를 자연스러운 한국어로 설명하도록 공통 system 지침을 사용한다. 내부 ref·UUID 등 기존 유출 검증은 유지하되 입력에 실제 존재하는 고유명사는 보존한다. root/slot/scope 같은 표현만으로 새로운 검증 실패나 추가 LLM 호출을 만들지 않는다. 공개 응답의 문장 정리는 Spring이 담당한다. 과거 legacy 요청 golden은 보존하고, 고정 main SHA에서 독립 포착한 upstream 요청 기준을 별도 fixture로 둔다. 자연어 이유·주체 연결·일반 검토 지침과 cache의 승인된 차이만 override로 검증하며 모델·추론 강도·user payload·schema·출력 상한을 함께 대조한다.
- `SettingCandidate.automatic_review_hold_reason`은 Spring이 자동 반영 보류 원인을 보존하는 nullable `VARCHAR(50)` 컬럼이다. Python은 매핑만 제공하고 비교 결과나 보류 원인을 직접 덮어쓰지 않는다.
- 자동 누적의 세계관 주체 연결 재시도가 끝난 후보는 `failureCode`와 빈 대상 목록으로 Spring에 전달한다. 응답의 `FAILED`를 정상 모호성 `AMBIGUOUS`와 구분하며 실패 후보를 새 임시 대상/근거 누적에 넣지 않는다. 선택적 캐릭터 연결 보강 실패는 보강이 필요했던 후보만 최초 저장 시 `FAILED`와 `preparation_failure_stage=SUBJECT_RESOLUTION`로 기록하고 규칙으로 연결한 후보·발견 후보를 보존한다. 보류 문장과 journal은 Spring이 만든다.
- 세계관 신뢰도는 추출·매핑·게시 모든 단계에서 필수 유한 숫자 0~1의 원값을 허용한다. 0.9 같은 정상 값을 특정 등급으로 올리거나 내리지 않는다. NaN/Infinity/범위 초과는 허용하지 않는다.
- 후보 실패 분류는 계정 인증/권한/결제·비재시도 4xx를 제외한다. 영구 provider 실패가 일반 LLM_PROVIDER_ERROR 보류로 오인되지 않도록 격리 불가능한 코드로 보고한다. 짧은 metering 재시도 정책은 유지하며 새로운 무제한 반복이나 실패 후 전체 회차 건너뛰기는 하지 않는다.
- 누적 저장소·필터·journal 완료 여부는 Spring이 소유한다. AI는 원본 후보와 검증된 비교 결과를 기존 경계로 전달하며 정식 설정을 자동 확정하거나 누적 journal을 직접 갱신하지 않는다. 자동 모드의 실패 후보도 실패로 남기며 정상 반영되었다고 간주하지 않는다. Spring이 실패 참고 coverage와 정상 후보 자동 반영을 함께 검증해야 회차 완료가 가능하다.
- 회차별 자동 반영도 `ORDERED_PROVISIONAL`의 고정 입력 계약을 사용한다. Spring은 앞 회차 반영 뒤 현재 설정을 다시 캡처하며, `analysisContext.unresolvedReferences`에는 입력 hash에 포함된 앞 회차의 보류 주장만 전달한다. Worker는 이를 추출·주체 해소·양쪽 비교의 별도 `UNRESOLVED` 참고로 읽고 현재 snapshot이나 선택 가능한 대상에 합치지 않는다. 내부 UUID와 `HUMAN_REJECTION_POLICY`는 참고 DTO/prompt에 허용하지 않으며 필수 문맥을 입력 상한 계산에서 제외하거나 임의 절단하지 않는다.
- 범위 검토의 후속 참고에는 원본 `scopeName`/`settingName`과 기존 `matchedScopeName`/`matchedPropertyName`을 보존한다. prompt에는 non-null 경로 필드만 snake_case로 추가해 기존 참고 문맥의 형식을 유지하며, matched property가 있고 matched scope가 생략되면 기존 root 경로를 뜻한다. 이 참고는 계속 `UNRESOLVED + applied_to_current_state=false`다.
- ordered의 `knownCharacters.aliases`·`identityEvidence`와 항목별 `reviewSource=HUMAN|AUTOMATIC`은 이름 연결 근거와 검토 주체를 전달한다. 같은 인물로 연결된 발견 후보도 원본 근거와 실제/임시 대상 연결을 보존해 Spring이 확정 발견 기록으로 별칭을 누적할 수 있게 한다. 같은 청크의 중복 발견은 같은 임시 anchor를 유지하고, 후속 청크 대상 목록은 해당 인물의 이름·근거만 보강한다. 모호한 발견은 별칭으로 합치지 않는다. 기존 `CONFIRMED_ONLY`의 중복 발견 제거와 provider 요청 형식은 유지한다.
- ordered 재시도는 추출 checkpoint와 성공 batch를 보존하고 비교 stage의 pending 후보만 다시 claim한다. 이미 넘은 checkpoint를 역행 보고하지 않는다. 실패한 원본 LLM 응답의 일부 decision을 검증 없이 적용하지 않는다. ordered 세계관의 제한된 복구 결과는 검증된 decision과 typed failure가 모든 원본 source를 정확히 한 번 덮도록 함께 완료한다. 인물 ordered batch도 독립 검증을 통과한 decision과 복구 뒤 남은 typed failure를 함께 완료한다. `reviewMode` 생략/null은 `MANUAL`이며 수동 모드는 첫 실패에서 후속 batch와 다음 도메인 stage를 중단한다. 명시적인 `AUTOMATIC`은 ordered에서만 허용하며 비교 batch의 provider·응답 파싱·응답 검증 실패만 격리해 다른 batch와 다음 도메인을 진행한다. 원문·전체 추출 실패, 토큰 quota, 계정 인증/권한/결제 오류, lease, 고정 입력/대상 변경, Spring API 오류 및 예상하지 못한 오류는 자동 모드도 작업 전체를 중단한다. 자동 누적의 후보별 세계관 연결과 선택적 캐릭터 연결 보강 실패는 준비 실패 계약에 따라 원인을 보존하고 격리한다. `REVIEW_REQUIRED`/`EXCLUDE`는 검증된 처리 결과이며 실행 실패와 구분한다.
- 고정 입력/대상 계약 위반과 ordered 캐릭터 단일 후보·재시도 prompt의 입력 상한 초과는 `OrderedInputContextError`로 중단하고 batch 실패 API에도 격리 불가능한 `UNEXPECTED_ERROR`를 기록한다. Spring claim 거절·입력 상한 HTTP 오류도 같은 실행 오류로 분류해, 이미 저장된 context가 있다는 이유만으로 다음 claim/finalize가 통과하지 않게 한다. 원인 체인의 quota·lease·입력·Spring 오류는 비교 응답 오류나 불완전 작업 wrapper보다 우선하며, 세계관 대상 저장 검증의 기존 전용 코드와 Spring source metadata는 함께 보존한다.

- 운영 Worker 컨테이너는 Docker `journald` 로그 드라이버를 사용한다. Worker 서버의 systemd journal은 EC2 로컬 디스크에 최대 14일·1GB로 제한해 보관하며, 외부 로그 저장소와 애플리케이션 파일 로그는 별도 요구가 생기기 전까지 추가하지 않는다.
- 장기 실행 runner는 `AI_WORKER_CONCURRENCY`개의 실행 슬롯만 유지한다. 반드시 빈 슬롯을 확보한 뒤 Job 하나를 claim해 즉시 Task로 실행하고, 슬롯 없이 Job을 미리 claim해 프로세스 내부 대기열에 쌓지 않는다.
- 한 Job 안의 청크와 분석 stage는 순차 처리한다. `LLM_MAX_CONCURRENT_REQUESTS`는 프로세스 내부 provider 호출 상한이고, 동기 DB/S3 작업은 `AI_WORKER_BLOCKING_MAX_WORKERS`로 제한한 executor에 넘긴다.
- 회차 원문 청킹 기본값은 목표 6,000자·최대 7,000자·최소 1,000자다. 여러 회차 분석 요청도 Spring이 만든 회차별 Job에서 각각 같은 정책을 적용하며 한 Job 안에서 회차 원문을 합치지 않는다.
- 운영 `SETTING_EXTRACTION` 기본값은 분석 Worker 5개 × 프로세스당 동시 Job 10개 = 최대 50개다. 별도 `character-comparison`과 `world-comparison` 프로세스는 각각 Job·LLM 동시성을 1로 유지한다. 50은 설정 추출 Job 용량이며 여러 프로세스와 재비교 Worker를 합친 provider 계정 전체의 분산 상한은 아니다. 50개 Job 부하 테스트에서 Backend·PostgreSQL·LLM 지표가 기준에 미달하면 Worker 5개는 유지하고 프로세스당 Job과 LLM 요청을 5개로 낮춰 최대 25개로 되돌린다.
- 운영 SQLAlchemy 연결 풀은 설정 추출 Worker마다 `DATABASE_POOL_SIZE=3`, `DATABASE_POOL_MAX_OVERFLOW=0`을 사용하고 두 비교 Worker는 각각 연결 1개로 고정한다. Spring HikariCP 10개를 포함한 전체 애플리케이션의 최대 PostgreSQL 연결 수를 27개로 제한하기 위함이다.
- 각 Job의 lease token, heartbeat, 토큰 예약·정산, 실패 상태는 Task별로 분리한다. 한 Task의 예외가 실행 중인 다른 Job을 취소하지 않으며 heartbeat도 Job별 독립 Task로 실행한다.
- 종료 신호를 받으면 신규 claim을 즉시 중단하고 `AI_WORKER_SHUTDOWN_GRACE_SECONDS` 동안 실행 중 Job과 heartbeat를 유지한다. 운영 내부 grace는 180초, Compose `stop_grace_period`는 210초로 두며, grace를 넘긴 취소 Job은 heartbeat를 중단해 Spring의 lease 회수 경로로 재처리한다.

## AWS S3

- `AWS_ACCESS_KEY_ID`와 `AWS_SECRET_ACCESS_KEY`는 둘 다 설정된 경우에만 boto3 client에 명시적으로 전달하고, `AWS_SESSION_TOKEN`이 있으면 임시 자격 증명의 일부로 함께 전달한다. access key와 secret key가 모두 있지 않으면 기본 credential provider chain을 사용하며 실제 비밀값은 저장소에 커밋하지 않는다.

## Python Packaging

- 평가 fixture의 기본값 필드 제외는 Pydantic `exclude_if`를 사용하므로 `pydantic>=2.12.0`을 직접 의존성으로 유지한다. 기본 `stage2Policy`가 직렬화되어 기존 fixture hash가 바뀌지 않아야 한다.
- setuptools package discovery는 `app*`로 제한해 루트의 `samples`, `docs`, `scripts`를 배포 패키지에서 제외한다. `pyproject.toml`이나 루트 디렉터리를 변경하면 `python -m pip install -e ".[dev]"`로 editable install을 검증한다.

## Runtime Timezone

- SQLAlchemy가 PostgreSQL 연결을 만들 때마다 `TZ`를 session `timezone` 연결 옵션으로 전달한다. Amazon RDS 기본값이 UTC여도 공유 로컬 시간을 유지해야 한다.
- 운영 AI Worker는 Backend와 PostgreSQL의 `APP_TIMEZONE`을 `TZ`로 전달받으며 기본값은 `Asia/Seoul`이다. Python의 `datetime.now()`와 timezone 없는 공유 DB 컬럼이 동일한 로컬 시각을 사용하도록 이미지의 `tzdata`를 유지한다.

## Embedding Generation

- 신규 청크 임베딩 생성은 `EMBEDDING_GENERATION_ENABLED`로 제어하며 MVP 기본값은 `false`다. 비활성화 시 Embeddings client를 생성·호출하지 않고 설정 후보 추출과 Job 완료를 계속하며, pgvector schema와 임베딩 service·검색 코드는 후속 재활성화를 위해 유지한다.

## LLM Runtime

- 다단계 평가의 새 예측은 `processingVersion=1`로 표시하고 후처리 최종 후보마다 처리 기록을 정확히 하나 남긴다. 답지 대응과 실행 상태를 분리하며 누락·중복·decision 모순은 검증 오류로 처리한다. `diagnostics.json`과 Markdown은 같은 허용 필드만 사용하고, 구형 기록에서 확인할 수 없는 사유는 재실행 필요로 표시한다.
- 평가의 provider 중단은 후보가 없어도 회차 단위 `executionFailure`에 회차·단계·실제 호출 모델/용도·HTTP 상태·오류 코드/유형·매개변수·요청 ID·미완료 상태를 남기고 CLI와 공개 진단에 함께 표시한다. provider 메타데이터는 수집과 공개 시 허용 목록으로 정제하며 error.message·원문·응답 본문·요청 헤더를 기록하지 않는다. 알 수 없는 오류 식별자는 추측하지 않고 UNRECOGNIZED로 표시한다.
- 평가의 API 진단에는 실제 httpx 네트워크 예외 종류, 경과 시간, 출력 상한, 프롬프트 문자/UTF-8 바이트 수, 스키마 바이트 수와 입력 SHA-256을 남긴다. 오류 메시지는 알려진 원인 문구를 고정 요약으로 변환하고 원문을 보존하지 않는다. 시작/완료 로그와 실패 진단은 같은 입력 식별 기준을 사용하며 재현 실험에서 모델·입력·출력 상한을 동시에 바꾸지 않는다.
- 다단계 평가의 2차 상세에는 1차 과추출 후보도 실제 처리 결과 또는 결과 기록 없음으로 표시한다. 답지가 없는 진단 행을 Gold 기준 정확도에 넣지 않으며, 세계관 batch가 여러 후보를 한 decision으로 처리하면 전체 source 연결을 예측에 보존해 각 후보의 처리 결과를 추적한다.
- 다단계 품질 평가의 FIXED/ROLLING에서는 주체 해소 후 구체 이름이 있는 신규 인물의 설정에도 모델 출력 이름 기반 `prediction-character:` 식별자를 부여해 빈 기존 설정으로 2차 비교한다. 발견 후보와 설정은 같은 식별자를 쓰되 발견 자체는 2차에 보내지 않는다. Gold·1차 채점 결과로 실행 후보를 보충하거나 걸러내지 않으며, 미상·동명이인 등 실제 주체 모호성은 후보별 실패로 기록한다. 평가용 식별자는 이름으로 Gold와 대응시키고 채점·상태 집계용 target/removal 참조의 인물 부분만 변환한다. 원시 예측·선택한 설정 경로·값·기존 인물 ID의 오류는 보정하지 않는다.
- 다단계 평가에서 실제 주체 모호성 때문에 인물 연결 전 2차 비교를 요구하지 않는 캐릭터 `EXTRACT / SETTING` Gold는 `stage2Policy=WAIT_FOR_CHARACTER_MATCH`로 명시하고 연결된 2차 Gold를 두지 않는다. 구체 이름이 있는 신규 인물은 등록 ID가 없다는 이유로 대기 Gold로 바꾸지 않는다. 1차 추출은 계속 채점하며 정상 대기를 추출 실패로 세지 않는다. 정책은 해당 회차의 Gold 행에만 적용하고 canonical 인물 ID와 이후 회차의 이름 해소·매칭은 유지한다.
- 회차 종료 후 사용자 캐릭터 등록은 Scenario의 선택적 `registeredCharactersAfterEpisode`로 표현한다. 원문에 없는 이름을 CHARACTER_DISCOVERY Gold로 만들거나 `1차 제공 컨텍스트` 미리보기만 고쳐 입력을 바꾸지 않는다. 명시한 인물 ID·이름을 Gold·예측의 회차 종료 상태에 함께 반영하고 등록 자체는 모델 성과로 채점하지 않는다.
- 다단계 평가기의 세계관 의미 판정은 같은 분류·주체 안에서 설정 항목·상위 범위·설정값을 독립 채점한다. 정규화·검수된 별칭은 해당 축만 우선 인정하며, 다른 범위도 신규 ADD의 의미를 바꾸지 않는 묶음이면 문맥 판정으로 동등성을 인정할 수 있다. 1차 연결·2차·E2E에 같은 기준을 적용하되 기존 target·matched 경로·수정/병합의 경로 보존·root 이동 대상과 reducer 검증은 엄격히 유지한다.
- 캐릭터 평가는 서술형 값과 JSON 서술형 문자열, 같은 인물·factType의 동적 STATUS pattern 이름을 의미 판정한다. 인물 ID·factType·고정 key·숫자·불리언·target 및 제거 reference는 결정적으로 검증한다. 항목·범위·값을 판단할 수 없으면 해당 축을 PENDING으로 유지하며 승인된 대응은 일대일 평가용 키에만 사용한다. 원시 예측·reducer·상태 해시·실제 상태 적용 오류를 보정하지 않는다.
- 캐릭터 factKey의 마지막 한글 항목명에서 한글 사이 공백·밑줄만 다른 경우는 1차·2차·최종 상태 채점에서 같은 표기로 인정한다. namespace, 점으로 구분된 경로, 실제 단어·인물·factType과 영문·숫자 식별자는 합치지 않는다. 정규화는 평가용 대응에만 사용하고 원시 키·reducer 입력·상태 해시 및 중복 항목은 보존한다.
- Gold의 `acceptedFactKeyAliases`는 해당 행의 1차·2차·최종 상태 평가용 대응에만 인정한다. 인물·factType·값·target/removal과 exact reference 우선·일대일·중복 검증을 유지하고 원시 키·reducer·상태 해시는 보정하지 않는다.
- 세계관 consolidation은 실제 decision의 전체 linked sourceValues와 해당 Gold decision에 승인된 1차 통합 그룹·값 판정으로 채점한다. 동등한 내용의 SINGLE/MERGED 포장 차이는 인정하되 누락·미상·중복·다른 사실 source와 CONFLICT 대안 누락은 통과시키지 않는다. Gold·actual 모두 SINGLE인 정확한 단일 입력은 연결·identity 확인 후 값 오답과 통합 축을 분리하며 포장 차이·MERGED/CONFLICT의 의미 미판정은 PENDING으로 둔다. 원래 expected/actual enum과 독립 operation·target·path·값·상태 검증을 보존하며 개수 확인용 LLM 호출을 추가하지 않는다.
- 의미 채점기는 제품 모델과 독립적으로 `gpt-5.6-sol`·`medium`을 기본 사용하며 `--judge-model`·`--judge-reasoning-effort`로 주입한다. 채점 프롬프트 계약 변경 시 semantic outcome 캐시 버전과 `docs/multi-stage-setting-evaluation.md`를 함께 갱신한다. 의미 채점 judge의 자유 형식 reason은 공개하지 않으며, 점수·표·지표명은 진단 추가만으로 바꾸지 않는다.
- 평가의 2차 비교 이유는 사용자가 검토할 수 있도록 비교 AI의 `comparison_reason`과 실제 제안 처리를 제한된 진단 필드로 보존한다. 확인된 검증 거절 항목과 같은 묶음에서 처리를 끝내지 못한 항목을 구분하고, 나머지 항목이 정상이라고 단정하지 않는다. 보고서는 AI 판단·거절 이유·반영 영향을 한 번 읽고 이해할 수 있는 순서로 설명하며 내부 참조와 반복 설명은 줄인다. 저장되지 않은 과거 이유는 확인 불가로 표시한다. 응답 문자열은 데이터로만 취급해 표시 시 이스케이프하고, 프롬프트·원고 전문·인증값·예외 원문을 로그에 추가하지 않는다. 진단 때문에 비교 입력·재시도·판정·상태 적용·점수를 바꾸지 않는다.
- 비교 실패 이력은 실제 시도 요청의 후보 범위에 고정해 각 후보에 관련된 시도만 연결한다. 공개 JSON·Markdown의 사람이 읽는 설명은 내부 참조·UUID·처리 용어를 같은 기준으로 정리하고 정리 후 길이를 제한한다. 구조화된 코드·추적 ID와 비공개 원본 응답·예측은 유지한다.
- 의미 채점 요청은 회차 경계를 유지하고, 같은 회차 안에서 지시문·비교 데이터·응답 schema와 여유분을 포함한 입력 추정량 64,000토큰으로 묶는다. 8쌍 같은 고정 개수 제한은 두지 않으며 출력 상한은 추론을 포함해 요청당 32,000토큰이다. 출력 절단만 해당 묶음을 반으로 나눠 재시도하고 실패 호출의 사용량도 합산한다. 단일 비교가 입력 상한을 넘으면 호출 전에 거절하며 내용을 자르거나 후보를 누락하지 않는다.

- OpenAI Responses API 요청은 기본적으로 `store=false`를 명시한다. 운영 Worker·judge와 일반 평가는 이 기본값을 유지한다. 사용자가 원문/응답 저장을 명시적으로 승인한 진단 실행에만 다단계 평가 CLI `--store-responses` 또는 해당 실행의 `store_responses=true`를 사용한다. 실행 옵션 기본값은 false이고 전역 환경설정으로 저장을 활성화하지 않으며, 저장 여부와 응답 ID만 공개 로그에 남긴다.
- OpenAI Responses 기본 client는 응답 읽기 제한을 300초, 연결·전송·풀 대기를 각각 120초로 둔다. 읽기 제한은 AI 추론 시간이나 출력 토큰 상한이 아니며 주입한 HTTP client의 timeout은 덮어쓰지 않는다. 120초 뒤 클라이언트가 중단됐어도 공급자에는 생성 결과가 남을 수 있으므로 두 상태를 구분한다.
- 평가의 `extraction_max_output_tokens`는 예측 step의 캐릭터 1차 상한만 기본 6,000 또는 진단 9,000으로 선택하는 별도 override이며 운영 기본값과 구분한다. 절단 재시도는 runtime의 `LLM_SETTING_EXTRACTION_RETRY_MAX_OUTPUT_TOKENS`를 따른다(현재 기본 64,000). 과거 12,000 재시도 실행 조건·결과는 보존하고, 읽기 제한과 함께 바꾼 실험은 단일 변수 대조로 해석하지 않는다.
- 캐릭터 Fact·세계관 후보의 1차 추출은 `LLM_EXTRACTION_MODEL`, 캐릭터·세계관 주체 해소는 `LLM_SUBJECT_RESOLUTION_MODEL`, 후보와 확정 데이터 비교는 `LLM_COMPARISON_MODEL`로 독립 주입한다. 2026-09-09 사용자가 확인한 운영 라우팅은 추출·비교 `gpt-5.6-sol`, 주체 해소 `gpt-5.6-terra`다. 개별 값이 없으면 기존 `LLM_MODEL`(기본 `gpt-5.6-terra`)을 fallback으로 사용한다.
- 캐릭터·세계관 2차 비교·재비교 prompt에는 Backend가 반환한 1차 `evidenceSpans`를 읽기 전용 문맥으로 전달한다. 2차 LLM이 quote·offset을 다시 생성하거나 비교 완료 payload로 반환하지 않으며, 원고가 바뀐 경우에만 새 1차 분석 후보와 근거를 만든다.
- 운영 세계관 후보는 Spring 게시 전에 정규화한 `category + subject_name + scope_name + setting_name`별로 하나로 통합한다. `scope_name`은 세계관에만 있는 선택적 1단계 범위이며 빈 값은 루트 property를 뜻한다. 같은 설정명이라도 범위가 다르면 통합하지 않고, 운영 2차 비교의 기존 속성 선택은 반드시 범위+설정명 전체 경로를 정확히 매칭한다. 2차 비교는 추출값 하나면 `SINGLE`, 여러 값이 양립하면 `MERGED`, 동시에 참일 수 없으면 `CONFLICT`로 판정한다. `MERGED`만 자연스러운 최종 문자열 하나로 정리하고 `CONFLICT`는 모든 추출값을 그대로 보존해 사용자 판단으로 넘긴다. 각 1차 후보의 quote·offset과 raw payload는 어느 상태에서도 수정하지 않는다.
- 종족의 서술형 전투 특징은 `RACE / 종족명 / 전투 특성 / 마법 재능·신체 능력·전투 강점`으로 구분한다. 체력·힘·신체 능력에 따른 장비 착용 설명은 `신체 능력`을 보충하되, 독립된 수치 능력치·판정 규칙은 합치지 않는다. 원문에 있는 하위 속성만 추출하며 기존 경로·raw scope 검증을 우회하지 않는다.
- `POWER_SYSTEM`은 마법·스킬·능력 자체의 조건·자원·효과·제약을 설명할 때만 사용한다. 특정 능력과 무관한 세계·게임 공통 사망·전투·진행 규칙은 `WORLD_RULE_HISTORY`, 종족의 선천적 적성은 `RACE`로 유지한다. 분류 경계 조정만으로 enum·주체 식별·채점 기준이나 답지를 변경하지 않는다.
- 공통 추론 강도는 `LLM_REASONING_EFFORT`로 주입하고 생략하면 앱·Worker Compose 모두 `medium`을 사용한다. 명시한 값은 우선하며 GPT-6 계열에서도 `reasoning.effort`에 전달한다. GPT-6 모델이 지원하지 않는 명시값은 공급자 오류로 드러내고 조용히 생략하지 않는다. 현재 운영·품질 평가 기준 `medium`은 CLI에도 명시한다.
- GPT-5.6 모델의 토큰 예약량은 `o200k_base` tokenizer로 계산한다. 사용하는 tiktoken 버전이 모델 별칭을 모를 수 있으므로 모델명 자동 탐지 실패를 byte 상한으로 방치하지 않는다.
- Responses API는 HTTP 200만으로 성공을 판정하지 않고 `status=completed`를 요구한다. `status=incomplete`와 `incomplete_details.reason=max_tokens|max_output_tokens`, 또는 JSON 파싱 실패와 `outputTokens == maxOutputTokens`가 함께 나타나면 `LLM_OUTPUT_TRUNCATED`로 분류한다.
- 출력 상한은 목적별 환경변수로 주입하고 모두 양수이며 provider 최대 상한 이하인지 기동 시 검증한다. 기본값은 캐릭터·세계관 추출 각각 32,000·절단 재시도 64,000, 주체 해소 2,000, 단건 비교 3,000, 세계관 batch 16,000, 캐릭터 batch 128,000, provider 상한 128,000이다. 캐릭터 batch는 개수 상한 없이 전체 입력 256,000토큰 이하로 분할한다. `gpt-6.1-sol` 캐릭터 비교는 생성과 같은 model·system/user·reasoning·structured schema를 `/responses/input_tokens`로 정확히 센다. 동일 입력은 bounded cache로 fit→생성 preflight→예약에서 재사용하며 전체 그룹을 먼저 확인하고 초과 때만 원문 순서 prefix를 이진 탐색한다. feedback·projected 상태·시점·이력이 바뀌면 다시 세며 count 실패·유효하지 않은 응답에는 byte/tokenizer fallback이나 생성 없이 Job 오류로 중단한다. 이 경로의 예약량은 정확 입력+출력 상한이며 다른 목적에는 count 호출을 추가하지 않는다. 기존 모델은 기존 tokenizer/보수적 추정을 유지한다. 출력 상한에는 reasoning과 visible output이 함께 포함된다. 예약 최소 상한 128,256은 실제 청구량이 아니며 실제 input+output 사용량으로 정산한다. 기존 환경변수의 명시적 값은 새 기본값보다 우선한다.
- 캐릭터 비교의 출력 절단만 실패 segment를 원문 순서의 절반으로 나눈다. 앞선 성공과 Q-ref·dependency·대표 시점·현재값·회복/종료 이력·evidence를 유지하고, 단일 절단은 `LLM_OUTPUT_TRUNCATED` 실패로 남긴다. 입력 크기 실패는 `COMPARISON_VALIDATION_FAILED`로 정확히 덮으며 필수 공통 문맥이 넘으면 남은 후보를 같은 실패로 덮고 해당 논리 묶음을 중단한다. 문맥을 자르거나 quota·stale·frozen/lease 오류를 성공으로 바꾸지 않는다. 세계관 batch의 contract-complete 최소 출력 예상치가 16,000을 넘으면 기존 `BATCH_LIMIT_EXCEEDED` 검토를 유지한다.
- 캐릭터·세계관 추출의 출력 절단은 동일 입력으로 각각 32,000→64,000으로 한 번만 확장한다. 두 번째 절단은 종료하고 일반 JSON 문법·schema 오류의 기존 재시도 횟수와 섞지 않는다. 확장 호출도 증가한 최대량을 먼저 예약하며 quota 예약이 거절되면 provider를 호출하지 않는다.
- BOOK 전체 원문 동명 identity 검증은 주체 해소 모델을 유지하고 최초 출력 상한에 주체 해소·캐릭터 추출 상한 중 큰 값을, 절단 재시도에는 캐릭터 추출 재시도 상한을 사용한다. 일반 주체 해소 기본 상한을 함께 올리지 않는다.
- provider 사용량이 포함된 실패·출력 절단은 실제 input/cached/output을 `FAILURE`로 정산한다. 로그에는 목적·시도·출력 상한·사용량·incomplete reason만 남기고 prompt, 원고, 응답 본문, 내부 인증값은 남기지 않는다.
- Worker가 Spring에 보고하는 실패는 `AnalysisFailureCode`를 반드시 포함한다. 분석과 비교 분류기는 토큰 부족·출력 절단·네트워크·provider·응답 파싱·비교 검증·lease 만료·예상 밖 오류를 구분하고 자유 형식 예외 문자열로 복구 정책을 결정하지 않는다.
- 공통 검증 오류 요약은 Pydantic 오류 타입 또는 `예외종류(origin=app.analysis.모듈.함수:줄)`만 기록한다. 예외 원문·frame 지역변수·절대 경로는 포함하지 않는다. `origin`은 가장 안쪽 분석 코드 위치이며 배포 이미지 SHA의 소스와 대조한다. 이 요약은 기존 로그와 Spring 실패 API의 `errorMessage`를 통해 DB 오류 컬럼에 보존하고, Spring 전용 `sourceErrorCode/sourceReasonCode`를 Python 검증 코드로 재사용하지 않는다.

## 누적 상태 평가 계약

- 세계관 EXCLUDE의 proposed 경로는 저장 위치가 아니다. 실제 원본 또는 선택한 실제 matched 경로를 보존하면 정리용 분류 차이만으로 감점하지 않는다. 실제 target/matched 전체 경로·내용·출처 통합은 별도 검증하고 임의 경로는 거절하며 raw 예측과 표시 경로를 고쳐 맞추지 않는다.
- 세계관 병합 예측의 상태 적용은 sourceCandidateIds의 실제 원본 후보를 모두 검증해 reducer에 전달한다. 대표 후보만 전달하거나 MERGED를 SINGLE로 바꾸지 않는다. 실제 입력 개수와 ORACLE Gold 고유 값 규칙을 구분하고, 명시적 proposedScopeName=null은 최상위 경로로 보존한다.

- 기존 ORACLE/FIXED/ROLLING의 입력·상태 적용·채점 의미는 유지한다. 실험 A는 외부 S0를 명시하는 COMMON_START, B는 Java가 검증·seal한 journal을 복원하는 ORDERED_PROVISIONAL/VALIDATED_PROVISIONAL로 별도 구분한다. 기존 ROLLING의 모든 예측 적용 정책을 B로 대신 쓰지 않는다.
- B의 다음 회차 입력에는 Gold state/identity/결과를 사용하지 않는다. `ordered_journal`의 읽기 전용 mirror와 `ordered_state_projection`이 실제 Backend namespace를 보존하며, Gold 대응은 raw trace를 바꾸지 않는 scorer-only view에서만 수행한다.
- Backend S0 references의 `HUMAN_REJECTION_POLICY`는 동일 원문 주장 반려를 이어받기 위한 비공개 해시 규칙이다. Worker prompt로 전달하거나 평가 current fact/history로 투영하지 않는다. 전체 journal hash 검증에는 보존하되 사실 projection은 이 kind를 명시적으로 제외한다.
- saved prediction 평가에서는 runtime input/output hash·source hash·run/generation·sequence·SEALED 상태를 검증한다. 실패 회차를 검토 의도로 바꾸거나 이후 회차를 실행한 것처럼 채우지 않는다. Java domain reducer revision을 runtime policy에 기록한다.
- DRAFT 정답지와 operation accuracy 평균을 FINAL 품질/완전 결정 일치율로 보고하지 않는다. 지표 분자·분모·평균 방식과 semantic judge 사용 여부를 명시하고 유료 실행은 별도 승인을 받은 경우에만 진행한다. 실행안과 현재 adapter 제한은 `docs/ordered-provisional-evaluation.md`를 따른다.
- 캐릭터 청크 간 반복의 사실 단위 채점은 원문·원래 예측 해시와 저장 청크 경계에 고정한 독립 검수 sidecar를 명시했을 때만 사용한다. 정확히 같은 현재 PROFILE 사실만 `REVIEWED_FACT`로 묶고 기존 후보 처리 지표·모든 후보/2차 진단·reducer와 상태 해시는 보존한다. `EXCLUDE`나 동일 문자열만으로 반복을 면책하지 않으며 불명확한 출처·시점·중간 변화는 묶지 않는다. 계약과 재현 방법은 `docs/multi-stage-setting-evaluation.md`를 따른다.

- ORDERED 평가에서 Java journal v1의 누락된 자료형을 Gold 또는 JSON 모양으로 추측하지 않는다. 시작 상태는 실제 exporter의 자료형 목록을 제공하고 이후에는 seal된 쓰기와 연결된 실제 후보 자료형만 평가 DTO에 반영한다. 원본 journal/hash를 변경하지 않으며 metadata 누락·충돌은 거절한다. 과거 저장 예측·#65 채점 기준을 역사 자료로 보존하고, 저장 예측의 전체 보고서는 main 05d9c2d에서 독립 포착한 기준을 보존하고, 요청 6종은 최신 main 4ed7e55의 git archive에서 독립 포착한 기준과 대조한다.

## Application Metrics (GH206)

- `app.monitoring.worker_metrics`는 CLI 프로세스별 독립 registry/exporter를 관리한다. `AI_WORKER_METRICS_ENABLED=false`, `AI_WORKER_METRICS_HOST=127.0.0.1`, `AI_WORKER_METRICS_PORT=9102`가 앱 기본값이며 운영 Compose가 명시적으로 활성화한다. 별도 FastAPI 실행 없이 같은 프로세스의 HTTP thread에서 `/metrics`를 제공한다.
- 공통 custom label은 `application=catchhole-ai`, `environment=local|prod`와 `worker_kind=analysis|character-comparison|world-comparison`이다. purpose/job_type/error/outcome은 고정 목록, model은 기동 설정/CLI의 허용 목록으로 제한하고 미등록 값은 other로 정규화한다. Job·작품·회원 ID/원고/prompt/응답/예외 원문/비밀값을 label에 넣지 않는다.
- 개별 LLM Histogram은 delegate 한 호출만 측정한다. semaphore·token 예약/정산·retry sleep을 제외하고 기존 providerLatencyMs 누적값을 하나의 호출로 기록하지 않는다. retries는 기존 transport loop의 실제 추가 호출이며 바깥 schema/truncation 재시도를 추정하지 않는다. cached input은 input의 일부, usage 누락은 별도 Counter로 기록한다.
- process_claimed 실행 시도와 canonical 회차 결과 준비는 다른 지표다. 후속 비교/부분 성공/사용자 재시도·lease recovery의 DB 결과는 Spring이 소유한다. 계측 오류는 기존 분석/ledger/slot 정리를 바꾸지 않는다.
- 분석 replica마다 다른 host metrics port를 쓰고 각각 scrape한다. 기본 host bind localhost, 운영은 사설 IPv4와 모니터링 SG만 허용한다. target 생성/전달·drain·rollback은 `deploy/WORKER_EC2_DEPLOYMENT.md`를 따른다. 현재 5×10 및 비교 각1 동시성을 변경하지 않는다.

### 채팅 실행 v1 (#237)

- 계약은 `docs/chat-execution-v1.md`, 양쪽 공통 fixture는 `docs/contracts/chat-execution-v1.json`이다. 질문/시도/provider 호출의 ID를 구분한다.
- Spring `domain/chat/{controller,dto,entity,exception,mapper,repository,service,type}`와 `global/config/chat`은 실행·권한·원장을 담당한다. 내부 wire DTO는 버전 경계를 함께 검토하도록 `ChatContract`에 record로 모으고 OpenAPI 이름은 `Chat*V1`으로 고정한다.
- Python `app/chatbot/{http_app,client,runtime,outbox,providers}.py`와 `app/schemas/chat_execution.py`는 독립 HTTP 진입점이다. 기존 Worker replica/lease loop에 HTTP listener를 추가하지 않는다.
- `CHAT_ENABLED=false` 기본. 공통 Spring YAML의 CHAT_* 설정은 local/prod 모두 환경변수로 지정한다. FastAPI는 별도 Compose로 격리하며 #258 이후 검증된 동일 publish SHA 배포에 연결한다. 운영 백필은 CHAT_OPERATIONS_ENABLED로 공개 게이트와 분리한다. 기능 flag를 꺼도 기존 예약 복구는 계속한다.
- 채팅은 실행에 묶인 tools route를 사용하며 member/work를 LLM 인자에서 받지 않는다. provider SDK 재시도도 실제 호출마다 `Execution.call()` 예약 경계 안에 있어야 한다.
- 실행 한도는 `EXECUTION_LIMIT_EXCEEDED`, 계정 잔액 부족은 `QUOTA_EXHAUSTED`로 구분한다. HTTP 429만으로 잔액 부족을 추정하지 않는다. 실패 안내와 재시도 판단에서 계정 상태와 실행 제한을 혼동하지 않기 위함이다.
- UNKNOWN은 사용량 0이 아니다. 원문 변경/삭제 시 저장 답변 자체도 무효화한다. 후속 대화 저장소가 생기면 같은 파기 계약을 연결한다.
- 이 변경은 모델/프롬프트/기존 분석 결과에 영향을 주지 않는다. 테스트 stub은 tests/support 전용이며 운영 handler로 사용하지 않는다.

### RDB 단일 질의응답 (#238)

- 계약·사례·한계·재현 명령은 `docs/chat-qa-v1.md`를 Java/Python에 동일하게 보관한다. 기존 `ChatResult`/조회 DTO를 재사용하며 현재값 계산과 권한 판정은 Spring이 소유한다.
- `app/schemas/chat_qa.py`는 질문 유형·답변 목적·작품 전체 collection과 요청 내 참조만 받는다. `qa.py`/`qa_lookup.py`는 대상·경로·필터 조회, `qa_collection.py`는 모집단·페이지 token 검사, `qa_synthesis.py`는 등록 자료의 소개·조건 판정·목록/집계를 맡고 `qa_render.py`가 실제 값·검증된 근거를 표시한다. 분류는 `chat-qa-plan-v6`, 경로 선택은 `chat-qa-field-select-v1`, 구형 행 선택은 `chat-qa-select-v3`이다. 현재 계약은 `docs/chat-query-quality-v1.md`, 후속 대화는 #241을 따른다.
- 현재값/이력의 저장 key와 scope를 모델이 추측해 조회하지 않는다. 새 분류 schema는 필수 focus로 고정 FactType과 질문의 항목 표현을 구분한다. 유형 전체는 먼저 필터하고, 동적 항목은 값 없는 FIELD 목록을 끝까지 확인한 뒤 F 참조를 선택해 실제 저장 경로로 조회한다. 필터된 결과는 전역 도구 예산까지 순회하고 모델의 10행 선택이나 입력 문맥 상한으로 자르지 않는다. 구형 기록 계획의 R 행 선택 경로만 기존 페이지/행 상한을 유지한다. 새 작품 전체 조건은 collection으로 현재 자료를 읽고 인물별 MATCH/NO_MATCH/UNKNOWN을 판정한다. 기존 CHARACTER_LIST 기록만 단일 scalar 정확 일치 경로를 유지한다. 이름은 현재 질문 또는 제한된 서버 대화 문맥에 명시된 문자열만 조회하며 복수 후보·불완전한 탐색 페이지는 추가 확인으로 끝낸다. 모델이 선택한 행의 의미적 적합성은 별도 품질 검증 대상이다.
- `qa_config.py`의 `CHAT_QA_*` 설정은 `.env`를 읽지 않고 프로세스 환경으로 주입한다. `ENABLED=false`, `MODEL=gpt-6-luna`, `REASONING_EFFORT=medium`, `MAX_ATTEMPTS=2`, `MAX_OUTPUT_TOKENS=1500`, `ANSWER_MAX_OUTPUT_TOKENS=3000`, `TRUNCATION_RETRY_MAX_OUTPUT_TOKENS=6000`, `MAX_INPUT_TOKENS=32000`, `MAX_CONTEXT_CHARACTERS=32000`, `PAGE_SIZE=20`, `MAX_PAGES=2`, `MAX_ITEMS=80`, `MAX_TOOL_CALLS=16`, `MAX_SELECTED_ROWS=10`, `MAX_EVIDENCE_CALLS=8`, `MAX_ANSWER_CHARACTERS=12000`을 기본으로 한다. 이름 앞에는 모두 `CHAT_QA_`를 붙인다.
- 독립 `compose.chatbot.prod.yml`이 위 설정과 `LLM_API_KEY`를 명시적으로 전달한다. `deploy/chatbot.env.example`에 예제를 유지한다. QA 활성화 시 key가 없으면 기동을 거절하며, 기본 비활성 상태에서는 provider client를 생성하지 않는다. Spring 기능 flag와 별도여서 명시적 운영 활성화가 필요하다.
- 질문 해석과 행 선택은 기존 `ChatTextClient`/`Execution.call()`을 사용한다. 최초 호출과 schema/참조 오류·provider 출력 절단 복구는 같은 MAX_ATTEMPTS를 공유한다. 절단은 해당 목적 기본 출력의 두 배를 복구 상한 안에서 새로 예약하며 JSON 조각을 이어 붙이지 않는다. 실패 사용량을 보존하고 모든 재시도도 새 호출로 예약·정산하며 자동 transport retry는 하지 않는다. 조회 오류를 빈 결과로 바꾸지 않으며 현재값/이력/출처 범위와 원문 확인 불가 상태를 보존한다.
- `app/chatbot/replay.py`는 네트워크·DB·API key 없는 합성 CLI 재현기다. `tests/fixtures/chat-qa-v1.json`의 고정 모델 출력은 코드 경로 검증용이며 배포 모델 품질 측정값으로 보고하지 않는다. 실제 HTTP 검증용 `tests/support/chat_qa_stub_server.py`는 `CHAT_TEST_ONLY=true`와 localhost 제한을 유지한다.
- 이 변경은 챗봇 분류·선택·출력 로직에 영향을 주므로 AI 로직 버전을 기록한다. 기존 분석 Worker의 추출·비교 프롬프트와 모델 설정은 변경하지 않는다.

### 원문 검색 준비 (#239)

- `app/retrieval`과 `docs/retrieval-index-v1.md`는 현재 회차 검색용 준비/계량 계약이다. 공통 문서를 Java와 동기화한다. 기존 분석용 `episode_chunks`/source_chunk_id를 검색 크기로 바꾸지 않는다.
- V76 `retrieval_sources`/`retrieval_chunks`는 현재 원문 key/version/hash와 `cl100k-800-100-boundary-v1`(최대 800토큰, 약 100토큰 겹침)을 보존한다. 원문 slice와 Unicode code point offset을 저장하며 tokenizer token bytes를 decode해 재조립하지 않는다.
- 기존 저장 원고의 검색 읽기는 Java 인용 검증과 같은 UTF-8 2,000,000 byte 상한을 적용하고 신규 업로드·수정 글자 수 정책과 분리한다. byte 상한 초과는 `SOURCE_UNREADABLE`, 실제 hash/출처 변경은 `SOURCE_CHANGED`로 구분하며 원문을 잘라 상한을 우회하지 않는다.
- 새 분석의 `RETRIEVAL_PREPARATION_ENABLED=false`는 검색 청크 준비만 제어한다. 별도 one-shot CLI가 기존 `EMBEDDING_GENERATION_ENABLED=false`로 벡터 생성 여부를 결정한다. 검색 준비는 기존 분석의 사용자 과금 경로를 호출하지 않고 OPERATIONAL_BACKFILL/RETRIEVAL_INDEX_SERVICE_PAID_V1로 기록한다.
- CLI는 명시한 작품/회차/입력·청크·시도·시간 상한을 유지하며 .env를 읽지 않는다. S3/DB/원장/임베딩 실사용은 dry-run과 구분한다. SQLite outbox를 먼저 복구하고 성공 벡터는 공유 PostgreSQL에서 판정한다. API 응답 후 DB 반영 전 종료의 중복 호출 가능성을 숨기지 않는다.
- 검색 소비자는 `retrieval_current_chunks`에 서버 범위와 policy/model/version 필터를 함께 적용한다. 모델·차원·tokenizer 변경은 기존 공간과 혼합하지 않고 명시적 재생성/전환 계획이 필요하다. 설정집/과거 버전은 지원 전 READY로 표시하지 않는다.
- 별도 `compose.retrieval.prod.yml`은 #258에서 명시적 범위·고정 campaign ID·누적 예산을 가진 one-shot timer에 연결한다. 기본 OFF이며 같은 ID의 예산은 재배포/재시작으로 초기화하지 않는다. 기존 27 + 챗봇2 + 백필2 + preflight1 = 32개와 별도 관리 여유를 실제 RDS 기준으로 확인한다. 기존 워커 수/pool은 자동 변경하지 않고 민감 상태/outbox 볼륨과 Spring 파기 경로를 유지한다.

### RDB·원문 결합 답변 (#240)

- 공통 계약은 양쪽 `docs/chat-hybrid-retrieval-v1.md`와 `docs/contracts/chat-hybrid-v1.json`이다. `app/chatbot/hybrid*.py`·`app/schemas/chat_hybrid.py`는 독립 rdbMode/sourceNeed를 해석하고 기존 RDB 경로, 직접 근거, 키워드·인접·벡터 검색을 제한된 정책으로 연결한다. 원문 필요가 있으면 RDB 미등록만으로 종료하지 않는다.
- `app/retrieval/{search_contracts,search_repository,search,evidence}.py`는 오류 검증에도 재사용할 수 있는 읽기 전용 공통 모듈이다. 회원·작품·회차 범위는 서버 문맥에서 만들며 임의 SQL·LLM 생성 ID/settingKey/scopeName을 받지 않는다. 전체 허용 코퍼스에 유효성 필터·정렬을 적용한 뒤 LIMIT한다. 연결 회차는 keyword 정렬 힌트이며 범위를 제한하지 않는다.
- 기존 `EpisodeChunkVectorSearchService`를 명시적 scoped adapter와 계량된 query embedding client로만 재사용한다. legacy `episode_chunks`나 전역 `.env`/provider default로 우회하지 않는다. 임베딩 없는 유효 텍스트는 keyword/인접에 사용하되 부분 준비를 표시한다.
- 검색 근거는 기존 Fact 참조와 다른 `RetrievalReference`로 전달한다. hybrid plan v6/answer v7은 요청 부분과 지원 구절을 보존한다. `hybrid_grounding.py`가 모델이 지정 passage에서 그대로 복사한 supports.quote의 유일한 위치와 원래 청크 offset을 검사해 실제 지원 참조에 연결한다. 다른 passage로 이동하거나 없는/모호한 구절을 허용하지 않는다. 공개 인용은 원래 청크 계약을 유지한다. 저장 전 Python fingerprint 및 Spring 실제 원문/receipt 검증이 실패하면 관련 설명 전체를 보류한다. 올바른 참조/지원 구절이 의미적 지지나 작품 전체 최초를 보장한다고 주장하지 않는다.
- `CHAT_RETRIEVAL_ENABLED=false` 기본, QA ON과 명시적 `CHAT_RETRIEVAL_DATABASE_URL`(postgresql+psycopg)이 있어야 사용한다. 검색 연결은 읽기 전용 transaction·pool 2/overflow 0·5초 SQL/연결/풀 상한이며 전체 DB 연결 예산에 포함한다. 배포 설정/기본 상한은 `hybrid_config.py`, 별도 chatbot Compose와 env 예제에 함께 유지한다.
- 검색 최대 2라운드·질문 임베딩 1회·텍스트 모델 6회(각 schema/참조 오류·출력 절단 복구 포함), 기존 서버/QA 전체 도구·실행 토큰 상한을 함께 적용한다. provider 사용량은 기존 실행 원장을 통해 예약·정산한다. 검색 metrics에는 고정 method/purpose/outcome만 사용하고 질문·ID·원문을 label로 넣지 않는다.
- 오프라인 fixture의 고정 모델 출력은 연결 검증용이다. 유효한 인용을 가진 무관한 주장의 negative control은 품질 통과로 집계하지 않는다. 실제 모델/judge API 없이 내부 의미 검토한 결과와 실제 모델 품질 측정을 구분하고 v0009 기록을 유지한다. 이전 이력 전체 조회 한계와 #247 OPT-09 보류를 이번 기능으로 해결했다고 표시하지 않는다.


## 일회성 작품 대화 (#241)

- `docs/chat-sessions-v1.md`를 Java/AI/Front에 동기화한다. 명시적 종료 또는 마지막 질문/답변 후 2시간 만료이며 읽기/탭 닫힘은 TTL을 바꾸지 않는다.
- 세션 종료는 본문·인용 삭제와 새 호출/게시 차단을 소유하고 기존 실행 원장의 늦은 정산은 유지한다. 원문 무효화와 작품/회원 파기에 같은 경계를 적용한다.
- 과거 대화는 지칭 해석용 비신뢰 문맥이다. 최대 6턴/답변 1,000/전체 8,000 UTF-16 단위와 생략 안내를 유지하고 이번 요청의 RDB·근거를 다시 조회한다. 사용자 입력의 세션/작품 ID를 권한으로 신뢰하지 않는다.
- Front는 생성 SDK를 사용하고 본문을 URL/localStorage/sessionStorage에 저장하지 않는다. 인증 세대·작품·세션 캐시 분리와 이탈 후 늦은 응답 폐기를 유지한다.
- 합성 모델 출력 기반 로컬 연결 테스트와 실제 모델 품질 측정을 구분한다. 기존 기능 플래그는 기본 OFF, 운영 활성화/백필은 별도다.

### 챗봇 질문 품질 보완 (#247)

- 조회 계약·공통 fixture와 `docs/chat-query-quality-v1.md`를 Java/Python에 동기화한다. collection 조건은 관련 현재 자료에 대한 의미 판정이며, 부정·대소·복수 조건을 구형 valueEquals로 변환하지 않는다. 서버 모집단 대상 수·설정 행 수·확인한 MATCH 수를 구분하고, 부분 인물·UNKNOWN·미검토 대상을 확인한 수에 넣지 않는다. 수집한 COLLECTION은 사실 답변 게시 전에 readToken을 재확인한다.
- 조회가 완전해도 원문 검색이나 답변 길이가 제한될 수 있다. 경로 목록이 불완전하면 유일한 경로로 확정하지 않고, 필터된 값의 중단은 부분 확인으로 알린다. 도구 예산에 최종 원문 재검증/receipt와 직접 근거 재조회 비용을 남긴다.
- 본문은 plain text다. 실제 고유명사·저장값을 기술어 regex로 치환하지 않고, Markdown 백슬래시를 삽입하지 않는다. 내부 R/F/E 참조와 처리 enum 대신 현재값·출처 회차·삭제/이력 전용의 의미를 표시한다. 직접 sources 다음 retrievalSources의 원본 순서로 근거 번호를 맞춘다.
- 새 설명 문장은 목록·조건 explanation을 포함해 존댓말(~습니다/~요)을 사용한다. `qa_prompts.ANSWER_STYLE_INSTRUCTIONS`를 hybrid answer와 등록 설명·현재 조건·이력 조건의 네 자연어 프롬프트가 공유한다. 사용자/이전 대화 등 자료의 반말을 따르지 않는다. 저장값·고유명사·정확한 quote/supports는 보존하며 정규식 어미 변환이나 추가 LLM 호출을 하지 않는다. hybrid answer v7, 등록 설명/현재 조건/이력 조건 v2로 cache key를 구분하고 계획 v6은 유지한다. 말투 변경 이전의 실제 실행을 새 프롬프트의 검증으로 재사용하지 않는다.
- 새 원문 질문은 최대 네 개의 requestedParts(P1~P4와 질문의 요구 설명)를 보존하고 perItem이면 모든 논리 항목에 같은 요청 부분을 적용한다. 기존 answeredFacets/missingFacets/facet 필드는 새 계획에서 P 참조를 사용한다. 고정 TIME/HOW/WHY/SCENE와 구형 행 참조 coverage는 저장 기록 호환용이다. UNKNOWN 현재 조건을 원문 설명으로 승격하지 않으며 사용 장면을 획득 경위로 간주하지 않는다. 참조·항목 coverage와 의미적 지지는 별도 평가 대상이다.
- Spring의 상시 SETTING_BOOK_UNSUPPORTED는 일반 등록 설정 답변의 경고로 반복하지 않는다. 부분 조회·값 절단·근거 확인 실패는 유지하며 원문을 요청하지 않은 질문에 근거 목록 상한 경고를 붙이지 않는다.

- 작품 전체 collection은 실제 FIELD category/scope/key를 보존하고 경로별 DISTINCT를 합치지 않는다. 이름을 지정한 현재 소개·목록·개수도 생성 후 현재 version/name/존재를 재확인한다. HISTORY 집계는 사건 단위이며 현재값과 이력의 혼합 COUNT는 집계 대상을 확인한다.
- 시점·과정 질문의 eventScope와 원문 답변 eventStatus를 구분한다. 계획·불확실 판정을 실제 발생 완료로 세지 않고 계획 설명도 최종 근거 검증을 거친다. 항목별 검색 후보는 공통 앵커와 인접 문맥을 함께 배분하며 특정 인물·설정명에 맞춘 분기를 추가하지 않는다. 의미 분류 자체의 정답률은 별도 평가 대상이다.
- 새 requestedParts 일반 원문 답변은 회차별 교대 대신 검색 순위를 보존하고, 인접 확장은 재검증된 seed 순위·거리 순서를 따른다. 직전 검색 답변이 NONE이면 다음 벡터 라운드의 후보 목록을 새 결과로 교체한 뒤 인접 문맥을 확장하며 이전 키워드 점수로 새 후보를 밀어내지 않는다. PARTIAL의 검증된 답변·근거 snapshot은 보존하고 병합한다. 기존 회차별/개수/문맥 한도는 유지한다. 검색에서 회수됐는지, 모델 입력에 포함됐는지, 질문의 최초·동일 사건 조건에 의미적으로 연결됐는지를 별도 검증한다.

- 이름을 지정한 세계관 대상도 worldCategory를 공통 resolve_subject에 전달하고 RDB/hybrid에서 같은 FIND 필터를 적용한다. CHARACTER에 분류를 넣거나 분류 불일치 응답을 허용하지 않는다. 명확화 후속 질문은 원래 요청과 최신 정정을 함께 해석한다. 원문 후속 질문의 eventContext는 같은 사건의 제약이며 이전 답변 값·회차를 사실 근거나 강제 범위로 재사용하지 않는다. eventMatch OTHER/UNCERTAIN을 요청 사건의 확정 답으로 게시하지 않는다.
- 조건 결과는 MATCH를 먼저 표시하고 UNKNOWN은 개별 이름/설명 대신 수로 요약한다. 미확인·미검토·부분 조회가 남은 상태에서 일치 대상 없음으로 단정하지 않는다. 기존 주장과 실제 등록값을 표현 정리 목적으로 변경하지 않는다.

- 챗봇 기본 검색 문맥은 직접 인용과 겹침 병합된 검색 본문 합계 6,000 cl100k 토큰이다. Luna의 로컬 tokenizer 미지원은 보수적인 byte 입력 추정으로 유지하고 Spring 누적 예약 기본값 240,000과 동기화한다. 성공/부분 원문 답변은 실제 미확인 요구를 본문에 설명하되 검색 후보·문맥 한도만으로 포괄적인 미검토 문구를 붙이지 않는다. 실제 데이터/원문 오류는 숨기지 않는다. 조립 집계는 고정 사유·개수·선택 토큰을 기록한다. 별도 내부 추적에는 attempt ID, 검색 방식/순위/점수와 청크 ID, 선택·입력 절단 후 실제 E 참조, 제외 사유, coverage만 남긴다. 원시 질문·원문·모델 응답·사건 문맥 설명·회원/작품 ID·키·예외 본문을 로그에 넣지 않고 고유 ID를 metric label에 사용하지 않는다. 최종 선택 로그를 결과 저장 성공으로 간주하지 않는다.

### OPT-31 후속 대화·조회 규칙 — 2026-10-15 현재

- 현재 프롬프트는 QA plan v7·hybrid plan v8/answer v10이다. v9까지의 실제 검증 기록과 v10의 합성 표시 회귀는 분리한다. OPT-31 초기 답변 v8 뒤 관련 맥락·검증 실패 복구를 추가했다. 앞선 말투 보완 당시 v6/v7 기록은 보존하되 현재 동작은 이 규칙과 공통 `docs/chat-query-quality-v1.md`를 따른다. [#247 OPT-31](https://github.com/catchhole-soma/catchhole-backend-java/issues/247) 후속 실제 모델 검증은 77dc74e의 6건, 최종 제품은 표현만 보완한 265fc68로 구분하며 이전 실행을 새 구현의 통과 기록으로 사용하지 않는다. 최종 Python 2,853 passed/54 skipped/기존 warning 1이며 실제 ANSWER에는 PARTIAL이 포함되어 정답률로 집계하지 않는다.
- 인사·감사·챗봇 도움말만 있는 요청은 planner의 GREETING/THANKS/HELP와 비어 있는 조회 필드를 검증한 뒤 고정 존댓말을 기존 ANSWER로 반환한다. 문자열 감지로 사실 질문을 가로채지 않는다. CONVERSATION_LIMIT는 내부 모델에 유지하고 공개 결과에는 CLARIFICATION에서만 표시하며 실제 데이터/원문 오류는 숨기지 않는다.
- sourceFallback은 허용 계획과 조회 지점의 typed gap, 정상 INSUFFICIENT_EVIDENCE, 명시한 대상, 완전한 조회와 남은 예산을 모두 확인한 뒤 같은 실행의 원문 경로를 재사용한다. 현재값/명시적 부정의 정상 답변, collection·집계·명확화·부분/절단·자료 변경/원문 오류·실행 실패는 자동 전환하지 않는다. 세계관 전체 이름의 완전한 빈 조회에서만 공백으로 구분된 해당 분류 표시어를 한 번 제거해 같은 분류로 재조회한다. 기존 후보·권한·분류·범위를 우회하지 않는다.
- 계획 복구는 requestedParts의 주체·대상·전후·인과 범위와 최초 획득 조건을 유지하고 schema 필드 경로·고정 규칙만 재전달한다. 형식/계약/JSON/출력 절단을 구분하며 최초 포함 두 번과 기존 예약·정산을 유지한다. 원시 질문·원문·모델 응답·거절값·비밀값·임의 예외 본문을 진단 로그에 저장하지 않고, 구조 복구 성공을 의미적 정답으로 간주하지 않는다.
- 일반 claim의 relatedFacets는 관련 원문 맥락이며 요구 충족이 아니다. 미답 요구는 missingFacets와 PARTIAL로 남기고 정확한 supports.quote·참조/위치·최종 인용 검사를 유지한다. hybrid.answer의 ResponseValidationFailure만 미검증 문장을 버린 NONE으로 복구해 남은 검색이나 검증된 PARTIAL을 보존한다. 전송/provider·출력 절단 복구·권한/조회 실패는 숨기지 않으며 계획·등록 답변의 실패 경계와 전체 실행 상한은 유지한다.
- hybrid plan v8은 최초 습득 항목의 HISTORY와 현재 보유 목록의 CURRENT를 구분하고 실제 최초 획득은 원문 PROCESS로 확인하도록 지시한다. 최초 등록/사용 기록을 실제 최초로 단정하지 않는다. 일반 원문 경로의 직접 근거 조회는 `3 * max_rounds + 2 + collection 검사 수`의 도구 여유를 남기며 기본 두 라운드는 검사 수 외 8회분이다. 전체 상한을 늘리거나 키워드·벡터 실행을 보장하지 않는다. 후속 실제 검증에서 HISTORY+PROCESS와 키워드·벡터 두 라운드까지 확인했지만 최초 획득의 입증은 미확인으로 남았다. 상세 호출량·중간 실패와 SHA별 범위는 공통 품질 문서에 보존한다.
- 실제 77dc74e 검증 6건에는 자연 발생한 모델 응답 검증 실패가 없었다. related 맥락 경로의 실제 확인과 typed 검증 실패 복구의 합성 회귀를 구분한다. 최종 265fc68는 같은 프롬프트·검색·검증에 안내 표현만 보완했으며 전체 자동 테스트·로컬 기동으로 확인했고 실제 모델을 다시 호출하지 않았다.

- 답변 v10은 기존 답변 호출에서 일반/항목별 `missingExplanations`를 생성한다. 고유한 missing facet에만 연결하고 구체적인 미확인 범위를 자연스러운 존댓말로 설명한다. 설명으로 coverage를 높이지 않으며 병합에서 해결된 요구, 검증에서 제거된 주장과 충돌 항목의 설명을 제거한다. 설명 누락은 기본 안내로 보존한다. 과거 저장 답변과 최종 NONE 경로는 유지하며 v9 실제 실행을 v10 품질 측정으로 재사용하지 않는다.

### 챗봇 운영 배포·자동 백필 (#258)

- `deploy/manage-ai-runtime.py`는 같은 SHA의 워커·챗봇·백필 파일을 환경/연결 사전 검사한 뒤 배포한다. `worker.env`의 공통 설정을 서비스별 env가 덮어쓰고 이미지 SHA는 배포 인자로 고정한다. 기존 custom 값과 비밀값을 보존하고 비밀값/Compose rendered config를 로그에 출력하지 않는다.
- `app.retrieval.backfill`의 `run/status/preflight/resume`은 고정 campaign 범위·모델·버전·전체 입력 상한과 영속 SQLite를 사용한다. active works 또는 UUID allowlist를 명시하며 키셋 순회·원문 재검증·완료 청크 건너뛰기를 유지한다. 정상 순회 후 신규/수정분을 다시 탐색한다.
- 호출 전 보수적인 입력 상한을 영속 차감하고 DB 저장까지 불확실 표식을 유지한다. 불명/실패 재시도 소진은 BLOCKED로 멈추며 명시적 resume도 예산을 환급하지 않는다. 유료 실행 없이 preflight를 검사하며 기존 실행과 병행해도 상태/outbox를 변경하지 않는다.
- 5분 systemd timer는 같은 one-shot을 중복 기동하지 않는다. 내부 작업·임베딩 deadline은 300초이며 프로세스 벽시계 상한과 구분한다. Docker 실행/정리를 포함한 호스트 호출은 780초, systemd는 900초와 종료 유예 45초다. 장애로 종료가 지연되면 다음 timer를 건너뛸 수 있다. 실행 중인 백필의 이미지/프로세스를 재배포로 중단하지 않는다. 배포 성공과 색인 완료를 구분하며 AI 서비스 재생성 전에 설정/연결 검증을 완료한다. `docs/chat-runtime-operations.md`가 최초 설정·상태/복구 운영 계약이다.
- 최초 배포는 `bootstrap-chat-aws.py` → `bootstrap-chat-runtime.py` → `app.retrieval.bootstrap`으로 기존 운영 설정을 보완한다. 현재 AI publish SHA와 Java의 공통 잠금 지원을 확인하고, 같은 VPC의 유일한 API 인스턴스 및 실제 부착된 API/Worker SG를 검증한다. Worker TCP 8082에 API SG만 허용하며 IAM·인스턴스 용량을 자동 확대하지 않는다. API 경로/사용자는 기존 `/opt/catchhole`·`ubuntu` 계약이며 Worker의 custom 경로와 혼동하지 않는다.
- 최초 자동 백필 정책은 기존 값이 없을 때 활성 작품의 현재 원문, stable `retrieval-v1-rollout`, 서비스 부담 누적 보수적 입력 상한 50,000,000이다. 재배포로 새 ID/예산을 만들지 않는다. 기존 범위·예산·모델·DSN과 초기 설정 이후 수동 OFF를 보존하고, 비밀값과 역할 생성 intent는 호스트의 0700/0600 비공개 파일로 영속화한다. 기존 동명 역할의 암호를 재설정하거나 전역 DB ACL을 변경하지 않는다.
- API env 준비·첫 공개는 Java 배포와 같은 `.api-deploy.lock`을 사용한다. 첫 공개는 지원 원문 순회 READY/정상 EMPTY, 미전송 보고 0, 불확실 호출 없음, 현재 handler/연결 검증 뒤에만 수행한다. 30분 내 완료하지 못하면 timer/진행 상태를 보존하고 공개를 보류한다. `status`는 outbox를 읽기만 하며 보고나 유료 호출을 실행하지 않는다.
