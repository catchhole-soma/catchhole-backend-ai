# Repository Guidelines

## Pull Requests

- PR을 작성할 때 `.github/pull_request_template.md`의 섹션과 체크리스트를 유지하고 실제 변경에 맞게 모두 채운다.
- 관련 Jira 이슈와 GitHub 이슈·PR을 본문에 연결하고, 리뷰어가 재현할 수 있는 검증 명령과 결과를 참고 사항에 기록한다.
- `main` 대상 PR은 `.github/workflows/test.yml`에서 전체 pytest를 실행한다. DB를 사용하지 않는 단위 테스트는 로컬 `.env`나 CI의 `DATABASE_URL`에 의존하지 않고 경계 의존성을 주입·mock한다.
- 운영 이미지 발행과 Worker 배포는 `main` push에서 시작된 `Publish AI Image` 성공 흐름으로만 실행한다. Worker 배포는 해당 publish run의 commit SHA가 현재 `main`일 때만 진행하고, 그 SHA로 Compose와 이미지 태그를 함께 고정하며, Backend `main` 최신 커밋의 API 배포 성공과 Spring health를 확인한 뒤 시작한다.

## AI Logic Version Records

- 결과에 영향을 주는 추출·주체 해소·비교·후처리·프롬프트·제품 모델/실행 설정 변경 PR마다 `docs/ai-logic-versions/`에 다음 `vNNNN.md`를 추가하고 목록을 갱신한다. 규칙과 양식은 해당 디렉터리의 `README.md`와 `TEMPLATE.md`를 따른다.
- 기본 품질 평가는 다단계 `FIXED` 모드에서 추출 `gpt-5.6-sol`·주체 해소 `gpt-5.6-terra`·비교 `gpt-5.6-sol`, 제품 `LLM_REASONING_EFFORT=medium`으로 실행한다. `LLM_MODEL` fallback은 `gpt-5.6-terra`다. 로컬 CLI에도 이 값들을 명시하며 다른 모드·모델의 실험은 실제 사용값으로 별도 기록한다. 로직 버전 번호와 평가 모드를 혼동하지 않는다.
- 기록에는 이전 버전, 변경 이유와 전후 동작, 복원 기준 전체 Git SHA, 구현 PR, 프롬프트 버전과 실행 설정을 남긴다. 머지 전 최신 main의 버전 번호와 코드 기준을 확인하고, squash/rebase로 SHA가 바뀌면 최종 복원 SHA를 문서로 보완한다.
- 평가 실행별로 실제 코드·채점기 SHA, 고정 입력 식별값, 모델·judge·실행 조건, 핵심 집계 점수와 미판정·실패 수를 기록한다. `null`을 0으로 바꾸거나 조건이 다른 점수를 개선폭으로 표시하지 않는다.
- 미측정·실패·부분 완료는 사유와 담당자·재평가 계획을 기록하면 머지를 허용한다. 이 상태를 성능 개선 검증으로 표시하지 않으며 원문·정답·개별 예측 보고서를 버전 MD에 복사하지 않는다.
- 문서·테스트만의 변경은 버전을 올리지 않는다. 채점기·Gold·judge만 바뀌면 같은 로직 버전에 새 평가 기록을 추가한다. 예전 로직 복원도 최신 main에서 새 PR과 다음 버전으로 남기고 기존 배포 흐름을 따른다.

## Spring Worker API

- 분석 runner는 claim의 `allowedJobTypes`를 명시한다. 기본 `analysis` 프로세스는 `SETTING_EXTRACTION`, 별도 `character-comparison`/`world-comparison` 프로세스는 각각 사용자 재비교용 `CHARACTER_FACT_COMPARISON`/`WORLD_SETTING_COMPARISON`만 claim해 서로의 작업을 가져가지 않는다.
- claim 뒤 상태 변경, checkpoint, 세계관 후보 API와 토큰 예약에는 `X-Worker-Lease-Token`을 전송하고, 장기 provider 호출 중에는 60초 주기로 heartbeat를 보낸다. lease가 만료된 응답을 우회해 DB 상태를 직접 바꾸지 않는다.
- `SETTING_EXTRACTION`의 재시작 경계는 `CHUNKS_READY → CHARACTER_CANDIDATES_SAVED → CHARACTER_COMPARISONS_FINISHED → WORLD_CANDIDATES_PUBLISHED → WORLD_COMPARISONS_FINISHED` checkpoint 순서를 사용한다. 완료된 stage의 외부 호출과 저장을 반복하지 않는다.
- 캐릭터 `setting_candidates` 저장은 기존 SQLAlchemy 경계를 유지한다. 세계관 `world_setting_candidates` 생성·비교 상태 저장은 반드시 Spring 내부 Worker API를 사용하며, Python이 `world_settings`나 세계관 후보 테이블을 직접 수정하지 않는다.
- Python은 `setting_candidates.comparison_status`의 최초 값만 저장한다. 후보 claim 이후 상태 전이, snapshot context/version 검증, 비교 결과 저장, `CharacterFact` append와 `WorkCharacter` snapshot 반영은 Spring이 소유하며 Python은 공유 DB를 직접 갱신하지 않는다.
- 캐릭터 batch 비교 prompt에는 Backend UUID 대신 source 후보 `C*`, 시작 snapshot `P*`, 후보가 만들 projected slot `Q*` 요청 로컬 참조만 제공한다. exact/alias와 비-STATUS pattern key는 고정하고 pattern STATUS만 의미가 같은 canonical key로 정규화한다. complete에는 resolved key와 `P*`/`Q*` target·removal, projector가 계산한 `C*` dependency, `contextToken`을 보내며 실제 ID는 노출하지 않는다.
- 캐릭터 batch는 같은 분석 Job의 동일 캐릭터·FactType 후보를 원문 순서로 처리한다. 각 source 후보는 정확히 한 decision 또는 typed failure로 덮고 서로 병합하지 않으며, 앞선 성공 decision만 메모리 projected snapshot에 적용한다. schema 재시도 뒤 batch 검증이 실패하면 같은 projected state에서 원문 순서 singleton fallback을 하고, quota는 fallback하지 않는다. stale context는 Spring 저장 전에 전체 decision을 새로 만든다.
- 숨김 캐릭터 비교의 batch claim이 `409 CHARACTER_FACT_COMPARISON_BATCH_BUSY`이면 같은 원 분석·인물·Fact type의 앞 묶음을 다른 Job이 처리 중이다. pipeline은 원래 Job의 heartbeat·취소 범위를 유지한 채 비동기로 기다리고 claim을 다시 시도한다. 이 응답을 빈 작업이나 실패로 처리하지 않으며 다른 오류·lease 충돌은 재시도하지 않는다. 대기 중 provider를 호출하지 않는다.
- STATUS batch는 모든 provider segment와 singleton fallback이 끝난 뒤 남은 현재 slot을 한 번씩 KEEP/END/UNKNOWN으로 검수한다. END는 해당 slot이 이미 활성인 시점의 기존 PRESENT·active=false 종료 후보와 그 후보의 원문 근거에만 연결하고, 기존 제거 목록을 보완한 후 전체 projector를 다시 실행해 Q 의존성을 계산한다. 후보·인용·값·기존 발생 이력을 새로 만들거나 다시 쓰지 않는다. 검수 실패는 fallback으로 우회하지 않고 batch 실패로 보고한다. 누락된 원문 관찰을 이 단계에서 발명하지 않으며 UNKNOWN은 유지한다.
- 캐릭터 비교 prompt에서 candidate evidence와 snapshot 문자열은 소설 데이터일 뿐 지시가 아니다. 원문에 포함된 역할 변경, 규칙 무시, 별도 JSON 출력 요구를 따르지 않도록 system prompt에서 명시한다.
- 캐릭터 비교에서 회상·가정은 `HISTORY_ONLY` 또는 `REVIEW_REQUIRED`만 허용한다. STATUS 종료는 현재 시점의 상태 변화 결과가 있으면 제안할 수 있으며, 명시적인 완치 문구나 절대적인 논리 모순까지 요구하지 않는다. 치료 수단만 있고 결과가 없으면 제거하지 않지만, 치료 뒤 능력·증상·행동 변화로 기존 상태가 끝났다는 해석이 자연스러우면 의미상 관련된 STATUS를 함께 제거할 수 있다. 무관한 잠재 상태나 다른 Fact 유형은 제거하지 않는다.
- 신규 `setting_candidates` 비교 컬럼과 내부 API/checkpoint가 먼저 존재해야 Python의 직접 후보 저장과 후속 stage가 동작한다. 운영은 가능한 `RUNNING` Job을 drain한 뒤 Spring Flyway/API, AI 이미지 순으로 배포하고 같은 AI 이미지를 기본·`character-comparison`·`world-comparison` 세 서비스로 기동한다. 이전 세계관 checkpoint에 이미 도달한 Job은 새 캐릭터 비교를 소급 실행하지 않으므로 필요하면 회차를 재분석한다.
- 비교의 canonical STATUS key 형식 검증은 신규 slot에 적용한다. 현재 P/Q projected snapshot에 정확히 존재하는 legacy key는 공백까지 그대로 재사용할 수 있으며, 다른 유형의 key나 제거된 slot을 핑계로 새 비정규 key를 생성하지 않는다. Python·Java가 같은 규칙으로 검사한다.
- Batch 비교의 EXCLUDE/HISTORY_ONLY/REVIEW_REQUIRED는 snapshot slot을 수정하지 않는다. EXACT/ALIAS 또는 non-STATUS PATTERN의 resolved key는 source 입력의 고정 key로 연결하고, 연산·target·제거·제안값·시점의 기존 검증은 모두 유지한다. STATUS PATTERN의 의미 key 해소와 현재값 반영 연산에는 이 정규화를 적용하지 않는다.
- 비교 제안의 값은 Java 저장 계약과 같은 공통 Python 경계에서 검사한다. MERGE도 schema의 STRING/NUMBER/BOOLEAN/JSON 타입을 바꾸지 않으며 AGE/LEVEL 정수 범위와 구조화 속성 제약을 유지한다. 잘못된 제안은 값 원문 없는 타입 피드백으로 재시도하고, 원본 의미나 연산을 강제 변환하거나 검증 전에 projected 상태를 바꾸지 않는다.
- 세계관 비교 prompt에는 Backend UUID를 노출하지 않는다. Worker가 만든 `S*`/`T*` 참조만 LLM에 제공하고, 실제 대상 ID·현재 property·version 검증과 `beforeValue` 산출은 Spring이 담당한다.
- 세계관 묶음 비교는 후보별 canonical 주체를 먼저 해소해 Spring에 원자 저장한 뒤 시작한다. Spring이 `analysis job + source episode + category + canonical subject key + normalized raw scope`가 같은 후보만 한 batch로 claim하며, Worker는 claim된 batch를 원문 이름으로 다시 묶거나 나누지 않는다.
- canonical 주체 해소에서 정규화한 이름의 exact 대상은 최대 20개까지 모두 Spring에 보내 `AMBIGUOUS` 판정을 맡기고, LLM이 고르는 fuzzy 후보만 최대 3개로 유지한다. exact 대상이 20개를 넘으면 DTO 생성 전에 명시적 비교 검증 오류로 중단하며 앞의 일부만 잘라 보내지 않는다.
- 한 세계관 batch는 독립 속성별로 여러 decision을 반환할 수 있다. 모든 `C*` 후보 ref는 decisions 전체에서 정확히 한 번만 사용하고, 같은 속성의 여러 source를 합친 decision은 검수·확정 시에도 source 전체를 한 원자 단위로 처리한다. singleton decision을 별도 단건 비교로 다시 호출하지 않는다.
- 세계관 batch의 독립 decision은 source가 하나여도 신규 `ADD`라면 2차 LLM이 제안한 canonical `proposed_scope_name`·`proposed_setting_name`을 보존한다. 단, raw와 다른 새 scope는 현재 ADD, 기존 scoped property, 또는 `existing_root_property_names_to_move`로 함께 옮길 실제 root property를 합쳐 서로 다른 최종 하위 속성이 둘 이상일 때만 허용한다. 범위명과 설정명은 같을 수 없다. 독립 decision끼리 같은 상위 scope를 공유해도 source를 한 decision으로 합치지 않으며, 기존 단건 비교의 raw path 보정 규칙을 batch decision에 적용하지 않는다.
- batch context stale은 batch 전체 비교를 다시 만들고, canonical 주체 해소가 stale이면 기존 batch를 닫은 뒤 주체 해소와 새 batch claim부터 제한 횟수 안에서 다시 수행한다. quota·lease 만료·oversized batch는 부분 decision을 남기지 않는다.
- 세계관 `comparisonReason`은 검토 화면에 그대로 노출되는 사용자 문장이다. `S*`/`T*` 참조, UUID, key, version, operation enum 같은 내부 용어를 저장하지 않으며, 모델이 대상 참조를 반환하면 실제 대상명을 사용한 자연스러운 한국어로 치환한다. 실제 target의 주체·범위·설정명이 내부 token과 같은 영문 단어라면 표시명으로 쓴 부분만 허용하고, 그 외의 내부 enum·key 노출은 계속 거절한다.
- 기존 속성과 의미가 같아 세계관 후보를 `EXCLUDE`할 때는 2차 비교 결과에 해당 `target_ref`와 실제 `matched_property_name`을 함께 반환한다. Backend가 비교 당시 기존값을 `beforeValue`로 보존해야 하며, 일시적 사건처럼 특정 기존 속성과 비교하지 않은 제외만 두 값을 비울 수 있다.
- 세계관 후보의 `scope_name`이 비어 있고 같은 `setting_name`의 기존 속성이 특정 scope 아래에만 있으면 기존 scope를 자동 상속하거나 concrete operation으로 통과시키지 않는다. 모델이 matched 경로 없이 root `ADD`를 반환하더라도 입력 target을 기준으로 범위 모호성을 다시 판정하고, cross-scope `UPDATE/MERGE/EXCLUDE`와 함께 `REVIEW_REQUIRED + SCOPE_UNRESOLVED`로 정규화해 기존 matched 경로와 후보의 root 제안을 Spring Worker API에 전달한다. 후보 scope가 명시됐거나 설정명이 다른 잘못된 match, 그리고 다른 concrete operation의 full-path 검증은 계속 거절한다.
- 분석 progress 요청은 표시용 `currentStep`과 대상 회차에 적용할 `episodeStatus`를 함께 보낸다. 자유 형식 문구에서 상태를 추론하지 않도록 `EpisodeProcessingStatus` enum을 명시적으로 직렬화한다.
- claim payload는 복수 `episodes`가 아니라 단일 `episode`를 받는다. 한 `AnalysisJob`은 한 회차만 처리하고, 회차 사이의 반복과 실패 격리는 Spring의 Job queue가 담당한다.
- 장기 실행 runner는 한 Job의 실패를 Spring에 보고한 뒤 다음 claim을 계속한다. 개별 분석 예외로 Worker 프로세스 전체를 종료하지 않는다.
- Spring token reserve의 HTTP 409는 응답 `error.code`가 `AI_TOKEN_QUOTA_EXHAUSTED`일 때 전용 비재시도 예외로 바꾼다. 이 예외를 만난 후보를 typed failure로 보고한 뒤 같은 Job의 다음 후보를 claim하지 않으며, 다른 실행 중 Job Task는 취소하지 않는다.
- `source_chunk_id`는 LLM 생성값이 아니라 Worker가 가진 `EpisodeChunk.id`를 source of truth로 사용한다. LLM 응답에 값이 없거나 잘못되어도 Pydantic 검증 전에 현재 chunk ID로 덮어쓴다.
- 설정 추출 prompt에는 claim의 `knownCharacters` 대표 이름만 전달하고 Backend 내부 매칭용 `characterId`는 노출하지 않는다. 원문에 명시된 미등록 이름은 `candidate_kind=CHARACTER_DISCOVERY`로 추출하고 설정 payload는 모두 `null`로 두며, 기존 이름과 매칭되는 발견 후보와 같은 분석 안의 중복 발견은 저장 전에 제외한다.
- `knownCharacters[].activeStatuses`는 회차 시작 전에 활성인 STATUS의 `factKey`와 nullable `factValue`만 포함하고 임의 절단하지 않는다. 1차 prompt에는 상위 대표 이름을 `characterName`으로 결합한 최소 문맥만 전달하며 UUID·value JSON·provenance·history는 노출하지 않는다. 기존 상태의 단순 반복은 재추출하지 않고, 치료 수단만으로 종료를 단정하지 않으며 실제 기능·증상·행동 변화의 근거만 후보로 남긴다. 같은 회차 projected 상태 누적은 이 목록의 책임이 아니다.
- STATUS 후보의 `value_json.active`는 존재하면 JSON boolean만 허용한다. candidate나 2차 proposal의 `active=false`는 현재 snapshot에 ADD/UPDATE/MERGE하지 않고 REMOVE 또는 비반영 판단으로 처리한다.
- 독립 STATUS 추출에는 현재 청크의 비어 있지 않은 줄을 문장 종결 경계로 나눈 원문 단위와 요청 로컬 `E*` 참조를 제공한다. 모델은 schema enum에 있는 `evidence_refs`만 선택하고 코드가 원문 인용·청크 내 위치를 연결한다. 모델이 인용이나 offset을 생성하지 않는다. 알 수 없거나 중복된 참조는 안전한 오류 코드로 재시도하고 한도 후 실패한다. 같은 문장이 반복되어도 선택한 위치를 유지하며 Worker는 STATUS의 정확한 원문 일치 위치를 검증한 뒤 회차 전체 offset으로 변환한다. 일반 추출의 기존 위치 해소는 유지한다. 서로 다른 상태의 관찰은 같은 근거 단위를 공유할 수 있다. 같은 상태의 현재 활성 관찰과 END가 같은 E를 공유하면 현행 시간 순서 검증은 실패하며, 이를 자동으로 분할하거나 위치를 합성하지 않는다.
- 회차 시작 `activeStatuses`에는 기존 snapshot의 active 원본 값을 전달하지 않는다. Spring이 현재 slot으로 선택한 factKey와 nullable factValue를 문맥으로 신뢰하며, 신규 후보·제안의 active 타입 검증을 legacy snapshot 값에 소급 적용하지 않는다.
- `CHARACTER_DISCOVERY`의 캐릭터 매칭은 `entity_name`만 기준으로 한다. `케닉의 넷째 아들 세룸` 같은 `raw_entity_mention` 안의 기존 관계자 이름을 발견 대상 캐릭터로 오연결하거나 subject fallback으로 재해석하지 않는다.
- 저장 시 유일한 기존 캐릭터 ID에 MATCHED된 후보의 entity_name은 해당 ID의 비어 있지 않은 대표 이름으로 맞춘다. 같은 인물이 이름별 확정 그룹으로 갈라지지 않게 하며, 입력 후보·raw_ai_result_json·raw mention·근거·값은 보존한다. 다른 matching 상태나 알 수 없는 ID를 임의로 정규화하지 않는다.
- 같은 분석 작업의 `SETTING` 후보는 확정된 캐릭터 ID 또는 정규화한 구체 이름, `attribute_name`, `value_type`, canonical `value_json`이 모두 같을 때만 저장 전에 중복 제거하고 더 높은 confidence의 근거를 남긴다. STATUS는 source chunk와 근거 위치·인용도 같아야 같은 관찰로 합친다. 종료→재발→종료의 첫 값과 마지막 값이 같아도 서로 다른 관찰은 보존한다. 값이 다르거나 주체가 `AMBIGUOUS`인 후보도 유지한다.
- STATUS schema가 있으면 일반 추출에 non-STATUS schema만 제공하고, 별도 STATUS 추출은 현재 청크 전체를 독립적으로 읽는다. 일반 초안을 STATUS 모델에 넘겨 누락과 통합 표현을 답으로 고정하지 않는다. 두 호출 모두 같은 모델·토큰 예약/정산·출력 절단 재시도 경로를 사용하며, STATUS 추출 실패를 일반 초안으로 성공 처리하지 않는다. non-STATUS와 캐릭터 발견은 일반 추출 결과를 보존한다. 독립적으로 끝날 수 있는 상태를 별개 key로 유지하고, 새 키는 공백을 `_`로 표기하되 기존 slot 키를 임의 변경하지 않는다.
- 과거 신체의 질병·체질·기능 제약은 같은 화자의 기억이라는 이유만으로 현재 STATUS에 이어 붙이지 않는다. 현재 신체·능력과 대비되는 원문이 시점 판단에 필요하면 그 현재 청크의 인용도 후보 근거에 보존한다.
- 가정·조건부 미래 위험을 현재 STATUS 노출로 만들지 않는다. 가정 이력을 남길 때는 조건·비현재 요약과 해당 원문 근거를 함께 보존하며 active를 생략한다. 미발생을 상태 종료로 해석하는 active=false로 변환하지 않고, 실제 발생한 다른 관찰은 별도로 유지한다. 비교는 후보 속성보다 원문의 조건·양태를 우선한다.
- 설정 추출 claim의 nullable `previousEpisode`는 같은 작품의 바로 전 회차 원문 메타데이터다. Worker는 원문 끝 최대 14,000자와 현재 회차 앞뒤 청크를 지칭·시점 판단에만 사용하고, 이전 회차를 재청킹하거나 그 후보를 저장하지 않는다. 이 발췌만으로 주체가 특정되지 않으면 미상으로 남긴다. 현재 청크에 없고 참조 문맥에만 있는 인용은 재시도 후 실패하며 현재 회차 근거로 저장하지 않는다.
- SETTING의 raw 주체가 지칭어·미상·빈 값이면 추출된 entity_name이 기존 이름이어도 청크 단위로 재검증한다. 이름 목록은 주인공 순서가 아니며, 이전 회차의 화자가 현재 장면/대사의 화자보다 우선하지 않는다. 미등록 이름처럼 보이는 종족·직책·외형 호칭도 문맥 검증을 거친다. 구체 entity/raw가 기존 인물에 유일하게 MATCHED되는 경우에만 우회한다. 기존 인물로 확인되면 대표 이름을 사용하고 신규 인물은 강제로 합치지 않는다. 주체 해소에는 이름만 전달하고 UUID는 제외하며, 후보의 값·근거는 수정하지 않는다.
- 회차의 모든 청크를 추출한 뒤 저장하기 전에 충돌 가능성이 있는 신규 이름쌍을 SAME_PERSON/DISTINCT_PERSON/UNRESOLVED로 정리한다. 부분 일치·동시 등장·raw 이름 연결은 검토쌍 선정 근거일 뿐 병합 근거가 아니다. 후보별 원문 근거·위치를 값 없이 전달하고 SAME/DISTINCT 모두 현재·직전 회차의 실제 인용을 검증한다. 동일인 확립 후에만 기존 K 또는 최초 근거 시점의 이름을 대표로 선택하며, 기존 exact/고유 MATCHED 후보와 값·근거·순서·source chunk는 보호한다. 불확실한 신규 후보는 미상으로 보존한다. 이 호출도 주체 해소 토큰 예약/정산에 포함하고 SAME 이름 정리와 판단 보류 건수는 각각 집계한다.
- 동일인 판단 보류로 미상이 된 발견 후보도 AMBIGUOUS로 저장해 검토할 수 있게 한다. 서로 다른 후보를 같은 미상 이름만으로 중복 제거하지 않는다.
- `SettingCandidate.value_json`은 `JSONB(none_as_null=True)`로 매핑한다. `CHARACTER_DISCOVERY`의 Python `None`은 JSON literal `null`이 아니라 DB check constraint가 요구하는 SQL `NULL`로 저장해야 한다.
- 캐릭터 비교의 canonical `REMOVE`는 `target_ref=null`, `removed_snapshot_refs` 1개 이상, proposal 없음으로 출력한다. candidate와 같은 key 또는 다른 key의 의미상 관련된 현재 STATUS를 요청 로컬 `P*` 참조로 하나 이상 끝낼 수 있지만 non-STATUS·unknown ref·비현재 후보는 거절한다. 기존 `REMOVE + targetRef` 하위 호환 정규화는 먼저 배포되는 Spring이 담당하며 Python은 신규 형식만 생성한다.
- `NUMBER`/`BOOLEAN` 후보는 Pydantic 경계에서 `value_json.value`의 JSON 타입을 검증하고 Mapper가 저장 `attribute_value`를 그 값의 canonical 표현(NUMBER 숫자 문자열, BOOLEAN 소문자 `true`/`false`)으로 만든다. LLM이 보낸 원래 표시 문구는 Mapper 변환 전 payload로 `raw_ai_result_json`에 보존하고, 비교 proposal도 Spring에 보내기 전 같은 canonical 규칙을 적용한다. 표시값과 snapshot 대표값이 다른 상태를 새로 저장하지 않기 위함이다.
- 캐릭터 단건·batch 비교의 `STRING` proposal은 `proposed_value_json.value`가 JSON 문자열인지 응답 재시도 경계에서 검증한다. 잘못된 값을 임의로 문자열로 바꾸지 않으며, 재시도 소진 시 기존 후보별 typed failure로 처리해 평가 최종 결과 생성까지 오류를 넘기지 않는다.

## Async Worker Runtime

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

## Character STATUS Evaluation

- STATUS 초벌 추출 뒤에는 현재 청크의 전체 E 근거와 시작 활성 상태·초벌 상태를 대상으로 별도 관찰 검수를 실행한다. 초벌이 놓친 기능 회복은 기존 후보의 인용을 바꾸지 않고 새 종료 후보로 만들며, 이름·key는 검수 대상에서 코드가 고정하고 active=false와 정확한 현재 근거를 연결한다. 후속 비교는 원문에서 빠진 근거를 복원할 수 없기 때문이다.
- 검수는 실제 발생·악화·과거·종료 관찰을 보존한다. ONE_OFF_TREATMENT_PROCESS는 실제 한 번의 처치 과정을 별도 지속 STATUS로 오인한 초벌만 현재 원문 근거와 함께 제외하며, 일시성·외부 환경 원인·독립 지속성 부족만으로 실제 기능 제약을 삭제하지 않는다. 현재 snapshot 적합성은 후속 비교가 판단한다. 실제로 지속되는 재생 효과나 독립 질병·저주는 보존한다. active=false 초벌은 preserved_end_observations에 읽기 전용으로 전달하고 코드가 원형을 보존한다. 원래 D 번호를 유지하며 수정 가능한 D와 모든 T의 전체·고유 응답, 현재 E 참조, 마지막 발생 이후 종료 순서를 검증한다. 읽기 전용 D를 분류 응답에 넣거나 검증에 실패하면 초벌 성공으로 우회하지 않는다.
- STATUS 초벌 하나에는 동일 상태의 한 발생·지속·악화·개선·종료 관찰과 그 근거를 연결한다. 뒤의 다른 관찰을 묶어 발생 시점을 뒤로 밀지 않는다. 기억 장애는 기존에 알거나 경험한 정보를 잃었는지 확인하고, 그 기억의 실제 회복과 새로운 신체·역할·신원의 최초 학습을 구분한다.
- STATUS provider는 states 아래에 원문 순서 observations를 반환한다. kind START/CONTINUE/CHANGE는 active=true, END는 false로 코드가 변환하고 PAST/HYPOTHETICAL에는 active를 넣지 않는다. 모델이 active를 함께 생성하면 거절한다. STARTS_IN_CURRENT_CHUNK 선언에는 별도 현재 START를 요구하며, END는 앞선 현재 활성 관찰의 마지막 근거 이후여야 한다. 이전부터 있거나 시작이 불명인 상태의 END를 known slot이 없다는 이유로 거절하지 않는다. 이 선언의 의미적 진위는 원문 검수 대상이다.
- 관찰 kind는 검증 후 후보의 private attribute에만 연결하고 provider 공통 후보·저장 JSON에 새 필드로 넣지 않는다. 과거·가정 관찰은 관찰 검수에 읽기 전용으로 보존하되 현재 target과 발생 anchor에서 제외한다. 같은 Job의 앞 청크 문맥에도 이 구분을 유지한다. 일반 설정과 Java 저장·비교·확정 계약은 그대로 사용한다.
- 같은 producer state의 관찰에는 `(source_chunk_id, state_index)`를 private group으로 보존한다. 검수에서 검증된 동일 group의 현재 D를 한 target으로 연결해 이미 있는 END를 다시 만들지 않는다. 이름만 같은 다른 state나 legacy group=None 후보를 이 규칙으로 합치지 않는다. group의 원본 청크·정수 index·주체/key/type 일치를 검증하고, 모델 입력·저장 JSON에는 이 내부 식별자를 노출하지 않는다. 평가에서는 kind/group을 후보와 같은 순서의 별도 감사 배열로 기록해 정확히 재생한다.
- 회차 시작 활성 또는 앞 청크의 마지막 활성 관찰에 연결된 현재 초안, 같은 검증 producer group의 보존 END보다 앞선 현재 초안은 `preserved_state_observations`로 원형 보존한다. 기존 부상의 호전 이력을 별도 치료 효과로 오인해 삭제하지 않도록, 독립 처치 분류 응답에서는 제외하되 종료 판단에는 원래 D 번호로 연결한다. 새 독립 처치 효과의 분류는 유지한다.
- JSON STATUS 관찰의 provider 값은 `name`만 받으며 `active`는 kind에서 파생한다. END의 첫 근거가 마지막 활성 관찰의 마지막 근거보다 뒤에 있어야 하고, 충돌 시 양쪽 관찰 위치를 반환한다. 모델이 임의의 기억 범위·원인 필드를 만들거나 뒤의 무관한 근거를 붙여 순서 검사를 우회하지 않도록 하기 위함이다. STRING/NUMBER/BOOLEAN과 공통 저장 JSON 계약은 유지한다.
- 같은 회차의 앞 청크에서 주체가 해소된 STATUS 관찰은 Worker의 Job 지역변수로만 누적해 다음 청크 검수에 읽기 전용으로 전달한다. 현재 D/E 근거나 반환 후보에 섞지 않고 미상 주체는 문맥 연결에서 제외하되 저장 후보 자체는 보존한다. 공유 extractor 인스턴스에 분석별 상태를 저장하지 않는다.
- 관찰 검수는 같은 입력으로 독립된 유효 응답 3개를 고정 수집한다. 다른 표나 재시도 피드백을 다음 표에 전달하지 않는다. 초안 제외·신규 종료에는 각각 2표 이상 동의가 필요하며, 첫 동의 응답의 설명과 근거를 그대로 사용하고 근거를 합성하지 않는다. 기존 종료 보존과 신규 종료 추가도 구분해 신규 추가를 지지하는 표 2개를 요구한다. 3표를 얻지 못하거나 합의 후 원래 불변조건이 실패하면 전체 청크를 실패시키며, 추가 표나 부분 합의로 우회하지 않는다.
- 이 검수도 기존 추출 모델·metered client·lease를 사용한다. 원문 참조 외의 quote/offset을 생성하지 않으며, 기본/확장 출력 상한과 실제 실패 사용량 정산은 기존 추출 계약을 따른다.

- 회차를 넘는 STATUS 안정성은 `evals/character_status_stability`의 분리된 PostgreSQL·Java API 환경에서 한 화 분석·확정 후 다음 화로 진행해 검증한다. 1차 추출만 실행하는 평가를 snapshot 반영 검증으로 보고하지 않는다.
- 평가 기대값은 모델 입력에 섞지 않고, 코드·원문 hash와 모델 설정 및 실패를 포함한 모든 시도를 기록한다. 원문·후보·근거를 포함한 실행 산출물은 비공개 `build/` 아래에 두며 커밋하지 않는다. 실행 완료와 별도 원문 대조 판정을 구분한다.

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
- 의미 채점기는 제품 모델과 독립적으로 `gpt-5.6-sol`·`medium`을 기본 사용하며 `--judge-model`·`--judge-reasoning-effort`로 주입한다. 채점 프롬프트 계약 변경 시 semantic outcome 캐시 버전과 `docs/multi-stage-setting-evaluation.md`를 함께 갱신한다. 공개 JSON 구조와 표·컬럼·지표명은 유지하고 판정 이유는 허용된 필드와 고정 문구만 사용한다. 모델의 자유 형식 reason은 공개하지 않는다.
- 의미 채점 요청은 회차 경계를 유지하고, 같은 회차 안에서 지시문·비교 데이터·응답 schema와 여유분을 포함한 입력 추정량 64,000토큰으로 묶는다. 8쌍 같은 고정 개수 제한은 두지 않으며 출력 상한은 추론을 포함해 요청당 32,000토큰이다. 출력 절단만 해당 묶음을 반으로 나눠 재시도하고 실패 호출의 사용량도 합산한다. 단일 비교가 입력 상한을 넘으면 호출 전에 거절하며 내용을 자르거나 후보를 누락하지 않는다.

- OpenAI Responses API 요청은 기본적으로 `store=false`를 명시한다. 운영 Worker·judge와 일반 평가는 이 기본값을 유지한다. 사용자가 원문/응답 저장을 명시적으로 승인한 진단 실행에만 다단계 평가 CLI `--store-responses` 또는 해당 실행의 `store_responses=true`를 사용한다. 실행 옵션 기본값은 false이고 전역 환경설정으로 저장을 활성화하지 않으며, 저장 여부와 응답 ID만 공개 로그에 남긴다.
- OpenAI Responses 기본 client는 응답 읽기 제한을 300초, 연결·전송·풀 대기를 각각 120초로 둔다. 읽기 제한은 AI 추론 시간이나 출력 토큰 상한이 아니며 주입한 HTTP client의 timeout은 덮어쓰지 않는다. 120초 뒤 클라이언트가 중단됐어도 공급자에는 생성 결과가 남을 수 있으므로 두 상태를 구분한다.
- 평가의 `extraction_max_output_tokens`는 예측 step의 캐릭터 1차 상한만 기본 6,000 또는 진단 9,000으로 선택한다. 절단 재시도 상한 12,000과 운영·다른 단계 상한을 유지하며, 읽기 제한과 함께 바꾼 실험은 단일 변수 대조로 해석하지 않는다.
- 캐릭터 Fact·세계관 후보의 1차 추출은 `LLM_EXTRACTION_MODEL`, 캐릭터·세계관 주체 해소는 `LLM_SUBJECT_RESOLUTION_MODEL`, 후보와 확정 데이터 비교는 `LLM_COMPARISON_MODEL`로 독립 주입한다. 2026-09-09 사용자가 확인한 운영 라우팅은 추출·비교 `gpt-5.6-sol`, 주체 해소 `gpt-5.6-terra`다. 개별 값이 없으면 기존 `LLM_MODEL`(기본 `gpt-5.6-terra`)을 fallback으로 사용한다.
- 캐릭터·세계관 2차 비교·재비교 prompt에는 Backend가 반환한 1차 `evidenceSpans`를 읽기 전용 문맥으로 전달한다. 2차 LLM이 quote·offset을 다시 생성하거나 비교 완료 payload로 반환하지 않으며, 원고가 바뀐 경우에만 새 1차 분석 후보와 근거를 만든다.
- 운영 세계관 후보는 Spring 게시 전에 정규화한 `category + subject_name + scope_name + setting_name`별로 하나로 통합한다. `scope_name`은 세계관에만 있는 선택적 1단계 범위이며 빈 값은 루트 property를 뜻한다. 같은 설정명이라도 범위가 다르면 통합하지 않고, 운영 2차 비교의 기존 속성 선택은 반드시 범위+설정명 전체 경로를 정확히 매칭한다. 2차 비교는 추출값 하나면 `SINGLE`, 여러 값이 양립하면 `MERGED`, 동시에 참일 수 없으면 `CONFLICT`로 판정한다. `MERGED`만 자연스러운 최종 문자열 하나로 정리하고 `CONFLICT`는 모든 추출값을 그대로 보존해 사용자 판단으로 넘긴다. 각 1차 후보의 quote·offset과 raw payload는 어느 상태에서도 수정하지 않는다.
- 공통 추론 강도는 `LLM_REASONING_EFFORT`로 주입한다. 현재 운영·품질 평가 기준은 `medium`이며 환경변수를 생략한 앱 설정의 기본값 `none`에 의존하지 않고 명시적으로 지정한다.
- GPT-5.6 모델의 토큰 예약량은 `o200k_base` tokenizer로 계산한다. 사용하는 tiktoken 버전이 모델 별칭을 모를 수 있으므로 모델명 자동 탐지 실패를 byte 상한으로 방치하지 않는다.
- Responses API는 HTTP 200만으로 성공을 판정하지 않고 `status=completed`를 요구한다. `status=incomplete`와 `incomplete_details.reason=max_tokens|max_output_tokens`, 또는 JSON 파싱 실패와 `outputTokens == maxOutputTokens`가 함께 나타나면 `LLM_OUTPUT_TRUNCATED`로 분류한다.
- 출력 상한은 목적별 환경변수로 주입하고 모두 양수이며 provider 최대 상한 이하인지 기동 시 검증한다. 기본값은 캐릭터 추출 6,000·절단 재시도 12,000, 세계관 추출 5,000·절단 재시도 10,000, 주체 해소 2,000, 단건 비교 3,000, 캐릭터·세계관 batch 비교 각 16,000, provider 상한 128,000이다. 캐릭터 batch는 Spring과 같은 기본 10개(요청 schema 방어 상한 20개), tokenizer 입력 상한 64,000을 사용하고 단일 후보도 넘으면 provider/fallback 없이 `COMPARISON_VALIDATION_FAILED` typed failure로 원자 완료한다. 세계관 batch의 contract-complete 최소 출력 예상치가 16,000을 넘으면 provider를 호출하지 않고 `BATCH_LIMIT_EXCEEDED` 검토로 전환한다.
- 캐릭터·세계관 추출의 출력 절단은 동일 입력으로 각각 6,000→12,000, 5,000→10,000으로 한 번만 확장한다. 두 번째 절단은 종료하고 일반 JSON 문법·schema 오류의 기존 재시도 횟수와 섞지 않는다. 확장 호출도 증가한 최대량을 먼저 예약하며 quota 예약이 거절되면 provider를 호출하지 않는다.
- provider 사용량이 포함된 실패·출력 절단은 실제 input/cached/output을 `FAILURE`로 정산한다. 로그에는 목적·시도·출력 상한·사용량·incomplete reason만 남기고 prompt, 원고, 응답 본문, 내부 인증값은 남기지 않는다.
- Worker가 Spring에 보고하는 실패는 `AnalysisFailureCode`를 반드시 포함한다. 분석과 비교 분류기는 토큰 부족·출력 절단·네트워크·provider·응답 파싱·비교 검증·lease 만료·예상 밖 오류를 구분하고 자유 형식 예외 문자열로 복구 정책을 결정하지 않는다.
- 공통 검증 오류 요약은 Pydantic 오류 타입 또는 `예외종류(origin=app.analysis.모듈.함수:줄)`만 기록한다. 예외 원문·frame 지역변수·절대 경로는 포함하지 않는다. `origin`은 가장 안쪽 분석 코드 위치이며 배포 이미지 SHA의 소스와 대조한다. 이 요약은 기존 로그와 Spring 실패 API의 `errorMessage`를 통해 DB 오류 컬럼에 보존하고, Spring 전용 `sourceErrorCode/sourceReasonCode`를 Python 검증 코드로 재사용하지 않는다.

- STATUS 발생/종료 근거 충돌의 재시도에는 검증된 두 관찰의 kind와 현재 E 참조만 별도 문맥으로 제공한다. 오류 위치만으로는 어떤 근거가 충돌했는지 재구성하기 어려워 같은 오류가 반복되기 때문이다. 원 Provider 응답·이름·값·인용·임의 필드는 재주입하거나 로그에 기록하지 않고, 매번 최초 입력과 직전 충돌 참조만 사용한다. schema/현재 E 검증 전 오류에는 이 문맥을 만들지 않는다.
