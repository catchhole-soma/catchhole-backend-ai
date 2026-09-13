# analysis

AI 분석 유스케이스와 분석 판단 로직을 두는 패키지입니다.

Spring 기준으로는 여러 하위 기능을 조합해 도메인 분석 결과를 만드는 Service/Domain Service에 가깝습니다.

## 역할

- 원문 청크를 입력으로 받아 캐릭터 설정·신규 캐릭터 발견 후보와 지속 가능한 세계관 속성 후보를 추출합니다.
- LLM 응답 JSON을 Python 내부 검증 schema로 확인합니다.
- 추출 결과를 `setting_candidates` 저장 구조에 맞는 중간 결과로 정리합니다.
- 근거 문장을 원문에서 다시 찾아 회차 전체 기준 위치를 계산합니다.
- 추출 후보의 캐릭터명 표현을 기존 캐릭터 목록과 비교해 매칭 상태를 계산합니다.
- 매칭된 캐릭터 후보와 현재 snapshot을 비교해 현재 화면 반영·검토·제거 제안을 생성합니다.
- 세계관 후보와 같은 category의 기존 대상을 좁히고 ADD/UPDATE/MERGE/EXCLUDE 제안을 생성합니다.
- NVM-143의 검증 근거 수집과 NVM-144의 충돌 판정은 후속 단계에서 연결합니다.

다음 책임은 Analysis에 넣지 않습니다.

- OpenAI HTTP 호출 세부 구현
- S3 원문 조회
- SQLAlchemy query 세부 작성
- Spring 내부 Worker API 호출

## 현재 파일

- `setting_extractor.py`
  - 청크 하나를 LLM에 보내 캐릭터 설정 후보와 이름 발견 후보를 추출합니다.
  - Spring claim DTO를 Worker가 변환한 immutable schema hint를 user prompt에 포함합니다.
  - claim의 기존 캐릭터 대표 이름을 user prompt에 포함해 이미 등록된 이름의 발견 후보를 만들지 않게 합니다. Backend 내부 매칭용 ID는 prompt에 포함하지 않습니다.
  - prompt 로드, user prompt 구성, 호출별 strict structured output schema 전달, JSON 파싱, 결정적 `source_chunk_id` 결합, schema 이중 검증과 안전한 교정 재시도를 담당합니다.
- `evidence_span_resolver.py`
  - LLM이 반환한 `evidence_spans[].quote`를 청크 원문에서 다시 찾아 offset을 보정합니다.
  - exact match를 우선 사용하고, 실패하면 공백/줄바꿈 정규화 기반 검색을 시도합니다.
  - quote를 찾지 못하면 잘못된 위치를 저장하지 않도록 offset을 null로 유지합니다.
- `character_name_resolver.py`
  - `KnownCharacter` 목록과 추출 후보의 `raw_entity_mention`, `entity_name`을 비교합니다.
  - 기존 캐릭터 하나와 확실히 연결되면 `MATCHED`, 후보가 없으면 `UNRESOLVED`, 대명사/복수 후보처럼 위험하면 `AMBIGUOUS`를 반환합니다.
- `character_subject_resolver.py`
  - `entity_name`이 비어 있거나 `미상`/지칭어 같은 구체적이지 않은 값인 후보를 `raw_entity_mention`의 형태와 관계없이 LLM으로 한 번 더 해소합니다.
  - 같은 current chunk에서 나온 fallback 대상 후보를 묶어 previous/current/next chunk 문맥과 함께 한 번에 전달합니다.
  - 설정 후보를 다시 추출하지 않고 주체만 판단하며, 정상 응답으로도 해소하지 못한 후보는 `entity_name="미상"`으로 보존합니다.
- `character_fact_comparison_schemas.py`
  - 캐릭터 비교 LLM 응답과 operation별 target/proposed/removal/temporal 불변식을 검증합니다.
- `character_fact_comparator.py`
  - legacy 단건 candidate와 현재 snapshot을 DB 식별자 없는 `P*` 참조로 비교합니다.
  - batch에서는 `C*` source·`P*` 시작 snapshot·`Q*` projected slot을 사용하며 source별 decision과 resolved canonical key를 검증합니다.
  - canonical slot, STATUS 제거, 시간 범위 규칙을 추가로 검증하고 내부 ref가 사용자-facing reason에 남지 않게 치환합니다.
- `character_fact_projection.py`
  - 같은 캐릭터·FactType batch의 성공 decision을 원문 순서대로 메모리 snapshot에만 적용합니다.
  - target/removal `P*`·`Q*`로부터 이전 후보 dependency를 결정적으로 계산하며 사용자 확정 전 DB는 바꾸지 않습니다.
- `character_fact_comparison_pipeline.py`
  - Spring에서 같은 캐릭터·FactType 후보 batch를 claim하고 context 조회, 원자 완료/실패를 조율합니다. legacy 단건 endpoint도 rollout 호환용으로 유지합니다.
  - 공개 `execute_character_fact_comparison_batch()`가 token 분할, 순차 projection, schema 실패 후 singleton fallback을 제공해 운영과 eval이 같은 규칙을 재사용합니다.
  - 정확한 stale error code일 때만 최신 snapshot context로 최대 3회 전체 batch를 다시 비교하고 후보별 검증 실패를 격리합니다.
- `world_setting_extractor.py`, `world_setting_schemas.py`
  - 청크에서 여러 회차에 재사용 가능한 종족·세력·장소·몬스터·능력 체계·규칙/역사·중요 아이템의 원자 속성을 추출합니다.
  - 현재 소유 상태, 날씨, 단발성 사건은 제외하고 confidence를 `0.65`, `0.80`, `0.95` 중 하나로 제한합니다.
- `world_setting_comparator.py`
  - normalized exact 대상이 없을 때 같은 category의 대상명만 `S*` 참조로 LLM에 전달해 최대 3개를 선택합니다.
  - Backend가 반환한 현재 속성 문맥은 UUID/version 없이 `T*` 참조로 LLM에 전달해 ADD/UPDATE/MERGE/EXCLUDE를 판단합니다.
  - 기존 속성과 중복되어 EXCLUDE하면 해당 `T*`와 실제 속성명을 검증해 Backend가 기존값을 저장할 수 있게 전달합니다.
- `ordered_world_batch_contract.py`
  - 누적 세계관 batch의 필수 nullable 선택 응답 schema와 기존 root/scoped 경로 충돌 지침을 제공합니다. 선택 ref를 입력 경로로 복원한 뒤 기존 도메인 model과 validator로 다시 검증합니다.
  - 재시도에는 허용된 오류 코드·필드·판단 index와 입력에서 다시 확인한 기존 경로만 전달합니다. 실패 응답이나 원시 예외를 복사하지 않으며 의미상 잘못된 판단을 자동으로 통과시키지 않습니다.
  - 이미 연결된 대상이 하나라면 임시 주체이거나 속성이 비어 있어도 모든 판단이 그 대상 참조를 보존해야 합니다. ordered 입력에 주체의 등록 상태를 별도로 표시하고, 누락 재시도는 `CANONICAL_TARGET_REQUIRED`와 입력에서 확인한 참조로 교정합니다. 누락을 Worker가 자동 보정하지 않습니다.
  - 빈 속성 목록과 같은 배치의 신규 ADD를 기존 matched 속성으로 오인하지 않도록 명시합니다. ordered에만 `SCOPE_MISMATCH` 검토와 오류별 경로 피드백을 제공하며, 확정 설정 기반 호출에는 공통 자연어 이유 지침만 추가하고 schema 생략 계약은 유지합니다.
  - 마지막 시도도 고정된 오류 코드와 허용된 필드·판단 index를 실패 요약에 남깁니다. 이 상세 요약은 ordered 세계관 batch에만 적용하며 응답값·원문·대상 UUID·알 수 없는 필드명·예외 문자열은 기록하지 않습니다.
- `world_setting_pipeline.py`
  - 미해소 후보의 canonical 주체 저장, canonical batch claim, 비교 문맥 조회, 결과 저장을 조율합니다.
  - 정확한 HTTP 409 `WORLD_SETTING_CANDIDATE_COMPARISON_CONTEXT_STALE`은 같은 batch의 새 문맥으로 다시 비교하고, `WORLD_SETTING_SUBJECT_RESOLUTION_STALE`은 기존 batch를 reset한 뒤 주체 해소와 새 batch claim부터 다시 수행합니다.
  - Backend 계약 검증 400은 같은 LLM 결과를 다시 생성하지 않고 `COMPARISON_VALIDATION_FAILED`와 원본 source code/reason을 분리해 후보에 기록합니다.

### Ordered 캐릭터의 제한된 부분 복구

묶음 응답 재시도 후에도 검증이 실패하면, 마지막 응답에서 원본 후보 수·참조·순서가 모두 정확한 경우에만
각 판단의 schema, 기존 비교 규칙과 순차 projection을 다시 검증합니다. `OrderedCharacterBatchRecoveryError`는
그 검증을 통과한 독립 판단만 메모리로 전달하며 실패 응답 원문을 저장하거나 로그에 남기지 않습니다.
파싱이나 coverage가 불명확하면 유지할 판단을 추정하지 않습니다.

실패한 후보와 같은 고정 key의 후속 후보는 다시 비교합니다. STATUS는 다른 key도 상태 정규화·종료에 영향을
줄 수 있어, 실패한 STATUS 뒤의 모든 STATUS를 다시 비교합니다. 그 밖의 독립 정상 판단은 추가 모델 호출 없이
유지합니다. 재비교는 후보별 한 번으로 제한하며, 처음부터 단일 후보였던 실패에 추가 복구를 붙이지 않습니다.
기존 Q 참조와 REMOVE 이후의 부재 dependency를 다시 계산하고, 실패 후보의 Q 참조를 사용할 수 없게 합니다.
잘못된 ADD를 UPDATE로 바꾸거나 원본 key·값·근거를 조용히 바꾸지 않습니다.

완료 요청은 모든 원본 source를 정확히 한 decision 또는 typed failure로 포함합니다. 수동 ordered 모드는
혼합 결과를 저장한 뒤 후속 batch와 다음 stage를 중단합니다. 자동 모드는 정상 판단의 반영과 실패 참고 보존을
Spring이 검증한 뒤 나머지를 진행합니다. quota·lease·고정 입력·Spring 오류는 부분 완료로 바꾸지 않으며,
stale 문맥은 기존 재시도 한도 안에서 전체 결과를 다시 만듭니다. legacy의 기존 singleton fallback은 유지합니다.

### 비교 완료 요청이 서버에서 거절된 경우

서버의 HTTP 400은 모델 응답 검증 실패와 다릅니다. Python 검증을 통과했더라도 완료 요청의 DTO·대상·의존 관계를
Spring이 거절하면 기존처럼 작업을 중단합니다. 이를 일괄 부분 성공으로 바꾸지 않습니다.
`clients/safe_http_diagnostics.py`는 실패 저장과 로그에 실제 status, 허용된 고정 서버 code, 기존 reason-code,
알려진 DTO field 경로만 남깁니다. 내부 URL·요청/응답 본문·details.message·거절된 값은 넣지 않습니다.
현재 Java가 constraint code를 제공하지 않으므로 오류 문장으로 규칙을 추정하지 않습니다.

48화의 원래 거절 요청/응답은 영속 저장되지 않았으므로 정확한 400 원인을 사후 확정할 수 없습니다.
새 요약은 이후 거절을 구분하기 위한 진단 보완이며 이전 실패 원인을 복원하는 기능은 아닙니다.
원래 응답 객체와 typed source code는 메모리에 유지하고, lease·quota·stale·Spring 오류의 제어 흐름은 유지합니다.

### 사용자에게 보이는 판단 이유와 보류 원인

`comparison_reason.py`의 공통 지침은 인물·세계관 비교 이유를 설정 내용과 근거의 관계로 설명하도록 합니다.
예를 들어 “같은 slot에 ADD할 수 없다” 대신 “같은 항목에 정보가 있어 함께 정리할지 확인이 필요하다”처럼
씁니다. 원문·값·근거·실제 고유명사는 수정하지 않습니다. 기존 내부 ID·참조 유출 검증은 유지하지만,
root/slot/scope 같은 표현만으로 유효한 판단을 폐기하거나 추가 모델 호출을 하지 않습니다.
저장된 과거 이유까지 포함한 공개 문장 정리는 Spring의 응답 경계가 담당합니다.

CONFIRMED_ONLY도 이 공통 system 지침만 추가합니다. 이전 요청 golden과 user payload·schema·cache를 보존하며
`legacy_reason_language_overrides.json`과 `legacy_subject_identity_overrides.json`으로 승인된
system 문구 변경만 별도로 검증합니다.
`SettingCandidate.automatic_review_hold_reason`은 Spring이 소유하는 nullable 문자열 매핑이며,
Python은 자동 반영 보류 원인이나 기존 비교 결과를 직접 갱신하지 않습니다.

### 세계관 batch 비교 계약

Worker는 미해소 후보를 먼저 조회하고, 전체 subject 페이지의 exact 이름 또는 `S*` subject resolution 결과를
후보별 target ID 목록으로 Backend에 원자 저장합니다. Backend는 저장된 canonical key를 사용해
`job + 회차 + category + canonical subject + raw scope`가 같은 후보만 batch로 묶습니다. Worker는 claim된 batch를
다시 주체별로 나누지 않으며, batch 안에서 독립 속성만 별도 decision으로 나눕니다.

ordered 주체 연결에서는 같은 분류의 입력 목록에서, 이름과 원본 근거가 같은 개념·개체임을 뒷받침하면
모델이 임시 주체를 명시적으로 재사용할 수 있습니다. 일반적인 게임 캐릭터의 장비 지표와 전투 지표는
서로 다른 속성이어도 같은 주체일 수 있습니다. 이름만 같다는 이유로 자동 병합하지 않고 다른 실체나
모호한 관계는 구분합니다. 연관된 분류나 상위·하위 종류도 같은 대상의 별칭은 아닙니다. 예를 들어 원문이
변이종·상위종·희귀종·상위 변이종을 각각 설명하면 구분합니다. 다른 이름을 연결할 때에는 별칭·약칭·번역이나
같은 대상을 달리 부르는 원문 근거를 확인합니다. 이 지침은 새 호출이나 다른 이름의 강제 보류를 추가하지 않습니다.
legacy 주체 선택의 “명백한 상하위 표기” 문구도 제거했지만, ordered는 그 파일 대신 별도 system 지침을 사용하므로
해당 문구를 ordered의 실제 의미 오판 원인으로 단정하지 않습니다. 한 대상을 명시적으로 선택하면 그 대상의 메모리상 연결 근거에 원본 근거만
중복 없이 보강해 후속 후보의 판단에 제공합니다. 첫 신규 anchor와 추출 원본은 유지합니다.
이전 회차의 보류 참고는 선택 가능한 주체나 확정 사실이 아니며, 새 회차 자체의 근거 없이 승격할 수 없습니다.

독립 decision은 source 후보가 하나여도 신규 `ADD`라면 2차 LLM이 제안한 canonical scope/name을 유지합니다.
다만 raw와 다른 새 scope는 현재 ADD와 기존 문맥을 합친 최종 하위 속성이 둘 이상일 때만 허용합니다. 기존 root
속성을 새 ADD의 형제로 옮겨야 할 때는 `existing_root_property_names_to_move`에 실제 root 속성명을 기록하며,
범위명과 설정명이 같거나 형제가 없는 단일 속성용 범위는 validation에서 거절합니다. 따라서 `생명력`,
`근력 기댓값`을 별도 속성으로 유지하면서 둘 다 `신체 능력` 범위 아래 정리할 수 있고, 공통 scope 때문에 source를
병합하지 않습니다. 단일 추출값과 evidence도 원본 그대로 보존합니다. legacy 단건 비교만 기존처럼
`ADD/EXCLUDE` 경로를 1차 raw path로 보정합니다.

각 batch decision의 `source_candidate_refs`는 입력 후보를 정확히 한 번씩 모두 덮어야 합니다. unknown/중복/누락
ref, 서로 다른 explicit scope 혼합, 잘못된 target ref, operation별 필드 위반은 부분 저장 없이 batch 전체
validation failure입니다. 완료 요청에는 decision, source ref coverage, canonical subject, target ID, context
version, raw comparison JSON을 보내며 원본 evidence/provenance는 Worker가 재작성하지 않습니다.

#### Ordered 기존 속성의 선택 참조

입력 `targets.properties[].ref`에 `T1.P1` 같은 요청 로컬 참조를 부여합니다. 모델은
`matched_property_ref` 하나로 기존 위치를 선택하고, 허용된 전체 ref 목록을 strict schema enum으로 받습니다.
Worker는 ref와 target_ref가 실제 같은 입력 대상에 속하는지 확인한 뒤 `matched_scope_name`과
`matched_property_name`을 복원합니다. 두 matched 경로 필드는 provider 응답에서 허용하지 않습니다.
UPDATE/MERGE는 proposed scope/name도 반드시 null로 반환하며 선택한 기존 경로로 함께 복원합니다.
ADD/EXCLUDE/REVIEW_REQUIRED는 실제 proposed 이름을 반환합니다. Backend complete DTO와
CONFIRMED_ONLY는 공통 자연어 이유 지침 외 기존 prompt·schema 생략·cache 계약을 유지합니다.

#### Ordered 대상의 빈 속성과 범위 검토

주체가 이미 연결되어 있다는 사실은 기존 속성이 존재한다는 뜻이 아닙니다. `properties=[]`인 임시 주체에는
`target_ref`를 유지한 ADD를 제안할 수 있지만, UPDATE/MERGE나 matched 경로를 가진 EXCLUDE는 불가능합니다.
일시적 사건 등 내용 자체를 제외할 근거가 있을 때만 matched_property_ref를 null로 둔 EXCLUDE를 사용합니다.
같은 배치의 다른 후보와 이번 응답에서 ADD할 속성도 기존 문맥이 아닙니다. 신규 사실끼리 중복되면 source를
보존해 하나의 decision으로 통합하고, 의미가 독립적이면 별도 경로로 ADD합니다.

기존 `SCOPE_UNRESOLVED` 자동 정규화는 범위가 없는 후보와 기존 scoped 동명 속성에만 적용합니다.
ordered에서는 이름이 다른 경우도 모델이 `REVIEW_REQUIRED + SCOPE_UNRESOLVED`를 명시적으로 제안하면
검토할 수 있습니다. 예를 들어 root 후보 `전투 특징`과 기존 `행동 및 사냥 방식 › 함정 사용`의 관련성이
불분명하면 원본 root 경로를 보존한 검토로 남깁니다. 단일 null-scope source와 실제 scoped 속성만 허용하며,
proposed scope는 null, proposed 이름은 원본 그대로, 이동 목록은 []입니다. 원본 값과 근거, 모델의 구체적인
관련성 설명을 보존하고 자동 반영하지 않습니다. 이름이 다른 concrete 연산의 실패를 자동 검토로 바꾸지는 않습니다.
검토 사유는 **원본 후보의 범위**로 구분합니다. 예를 들어 root 후보 `근접 무기 효과`와 기존
`전투 및 능력 › 독`의 관련성을 검토한다면 `SCOPE_UNRESOLVED`와 원본 null scope를 사용합니다.
기존 속성에 scope가 있다는 이유로 `SCOPE_MISMATCH`를 선택하거나, schema 오류를 없애려고
기존 범위를 proposed scope에 복사해서는 안 됩니다. 범위 불일치 및 SCOPE_MISMATCH schema 오류의
재시도 피드백도 이 구분을 안내하며, 부적합 응답의 거절 조건과 자동 정규화는 그대로 유지합니다.
`ORDERED_PROVISIONAL` batch는 별도로 모델이 명시한 `REVIEW_REQUIRED + SCOPE_MISMATCH`를 허용합니다.
예를 들어 후보 `외곽 지역 › 조명 환경`이 기존 `1층 › 광원`과 관련될 수 있지만 두 범위의 포함 관계가 불명확하면,
기존 값을 수정하지 않고 두 경로를 보존한 검토를 제안할 수 있습니다. 다음 조건을 모두 검사합니다.

- source는 명시적 scope가 있는 후보 하나이며, matched 속성은 공급된 canonical 대상에 실제 존재해야 합니다.
- matched scope는 source scope와 달라야 합니다. 기존 root 속성은 matched scope가 null이어도 됩니다.
- proposed scope/name은 원본 후보 경로와 같아야 하고, 기존 속성 이동 목록은 비어 있어야 합니다.
- 모델은 속성의 의미상 관련성과 확인할 범위 관계를 사용자에게 보일 한국어 이유로 설명합니다.

원본 값과 근거는 유지하며, 이 검토로 현재 설정을 자동 변경하지 않습니다. scope 이름이 다르다는 사실만으로
같은 속성임이 증명되는 것은 아닙니다. 다른 주체·없는 경로·무관한 속성이나 일반 오류를 검토로 자동 전환하지
않으며, 잘못된 UPDATE/MERGE는 계속 거절합니다. `CONFIRMED_ONLY`의 단건·batch에서는 새 검토 이유를 허용하지 않습니다.

후속 회차의 `analysisContext.unresolvedReferences`에는 원본 `scopeName`/`settingName`과 기존
`matchedScopeName`/`matchedPropertyName`이 선택 필드로 전달됩니다. Python은 non-null 경로만 snake_case로
prompt에 추가해 기존 참고 문맥의 형식을 보존합니다. matched property가 있고 matched scope가 생략되면 기존
root 속성을 뜻합니다. 이 문맥은 `UNRESOLVED`이고 `applied_to_current_state=false`이며 선택 가능한 target이 아닙니다.

#### Ordered 경로 오류의 재시도

캐릭터 ordered batch는 기존 canonical slot에 ADD하는 응답을 `CANONICAL_SLOT_ALREADY_EXISTS`로
식별하고 충돌한 C ref·현재 활성 P/앞선 Q ref·입력에서 확인한 key를 안내합니다. 예를 들어 기존
`profile.attribute`에 배경 정보가 있고 새 독서 습관도 같은 key로 추출됐다면 문장이 달라도 ADD는
불가능합니다. 모델이 두 정보를 보존할 MERGE나 안전한 검토 등 의미에 맞는 판단을 명시해야 합니다.
기존 값·근거·응답은 피드백에 복사하지 않고, 앞선 판단으로 P가 Q로 교체된 경우 현재 Q를 가리킵니다.
검증 조건이나 key를 바꾸고 응답을 자동 수정하는 방식은 아닙니다. 반복 실패 뒤에는 아래의 독립 검증과
제한된 부분 복구를 적용합니다. 공유 projection validator는 유지합니다.

| 검증 오류 | 피드백 코드 | 교정할 내용 |
| --- | --- | --- |
| 없는 property 선택 참조 또는 다른 target의 ref | `MATCHED_PROPERTY_REF_INVALID` / `MATCHED_PROPERTY_TARGET_MISMATCH` | 입력의 실제 속성 ref와 올바른 대상만 선택 |
| UPDATE/MERGE가 기존 proposed 이름을 직접 작성 | `EXISTING_PROPOSED_PATH_FORBIDDEN` | proposed scope/name을 null로 두고 선택 ref에서 복원 |
| EXCLUDE가 없는 기존 경로를 지정 | `MATCHED_EXCLUDE_PATH_NOT_FOUND` | 실제 기존 경로만 비교하거나, 내용상 제외 이유가 있을 때 matched 경로를 비움 |
| UPDATE/MERGE·검토가 없는 속성을 지정 | `MATCHED_PROPERTY_PATH_NOT_FOUND` | 입력 target의 실제 전체 경로를 선택 |
| 원본과 다른 새 scope의 하위 속성이 하나뿐 | `GENERATED_SCOPE_REQUIRES_SIBLINGS` | 실제 형제 조건을 충족하거나 root/원본 경로를 사용 |
| concrete operation의 matched scope가 source와 다름 | `SOURCE_SCOPE_MISMATCH` | 원본 범위를 유지하거나 조건을 충족하는 명시적 범위 검토를 제안 |
| 명시적 범위 검토의 source 수·범위·제안 경로·이동 목록 위반 | `SCOPE_MISMATCH_REVIEW_INVALID` | 단일 scoped 원본과 실제 matched 경로를 보존 |
| SCOPE_MISMATCH에 명시적 원본 scope 또는 matched 선택이 없음 | `SCOPE_MISMATCH_MATCH_REQUIRED` | null 원본에 scope를 만들지 않고, 관련된 단일 후보는 SCOPE_UNRESOLVED와 원본 null 경로를 명시 |

피드백은 최초 입력에서 재확인한 source 경로·target ref·기존 property index/경로와 고정 지침만 사용합니다.
실패 응답의 제안 경로나 원시 예외는 재주입하지 않습니다. 매번 모든 source를 포함한 전체 응답을 다시 검증하며,
재시도 소진은 검증 실패로 전달합니다. 세계관은 실패 원본 응답의 일부 decision을 성공으로 사용하지 않습니다. 정상 검토 판단과 provider·quota·lease·고정 입력 실패는 별도로 처리하고,
필수 입력 또는 재시도 prompt의 상한 초과는 `OrderedInputContextError`로 전체 작업을 중단합니다.

성공 반환의 raw JSON과 소진 예외에는 `validation_diagnostics`가 있습니다. 각 항목은
`attempt_number`, 고정 `rule_code`, 입력의 `candidate_refs`, `selected_properties`,
`allowed_matched_properties`를 포함합니다. 실제 선택 경로와 허용 제안 목록은 다르며, Spring 진단으로
보낼 때는 실제 선택 경로만 매핑합니다. 경로 항목은 `ref`, `target_ref`, `scope_name`, `setting_name`이며
원문·값·근거·응답 문자열은 포함하지 않습니다. 파싱 불가/출처 특정 불가 오류는 candidate와 선택 목록이
비어 있습니다. 마지막 실패 시도도 기록하며 진단 callback 오류가 원래 실패를 덮지 않습니다.

진단에는 선택 필드 `stage`와 `phase`를 추가합니다. stage는 응답 schema, 기존 속성 참조 복원, 개별 판단 검증,
원래 범위 계획, 정규화 뒤 범위 계획을 각각 `RESPONSE_SCHEMA`, `PROPERTY_SELECTION`,
`DECISION_VALIDATION`, `SCOPE_PLAN`, `PROJECTED_SCOPE_PLAN`으로 구분합니다. phase는 전체 비교 `BATCH`와
분리 복구 `RECOVERY`입니다. 기존 필드 생략/null은 허용하며, 사용자에게 보이는 판단 문장에 이 값을 노출하지 않습니다.

`ordered_world_rule_diagnostics.py`는 기존 고정 ValueError 문구만 진단 코드로 연결합니다. 예를 들어 같은 최종
경로를 두 판단이 제안한 경우 `FINAL_PATH_DUPLICATED`와 실제 입력에서 확인한 관련 후보를 남깁니다.
진단 전용 후보 참조는 복구의 실패 범위를 정하는 참조와 분리하여, 상세 정보가 추가됐다고 기존 전체 실패를
일부 성공으로 바꾸지 않습니다. 알 수 없는 오류는 포괄적 코드로 유지하며 원문·값·원시 응답·임의 오류 문장을
복사하지 않습니다. 마지막 시도, 분리 응답과 최종 합산 검증에도 단계와 진단을 보존합니다.

33화 형태의 통제 회귀는 전체 5개 후보 중 3개 정상·2개 실패, 전체 3회와 분리 4회의 호출 경계를 검사합니다.
이 테스트의 최종 경로 중복 응답은 합성 예시이며, 저장되지 않은 실제 거절 응답을 복원한 것이 아닙니다.
서로 다른 몬스터 분류의 분리와 명시적 별칭의 재사용도 통제된 선택 응답으로 검증하며 실제 LLM 정확도 평가는 아닙니다.
44화에서 관찰된 어둠의 근원/영생자, 일반 정수/수호자의 정수, 노랑/빨강 정수 이름도 반대 사례 fixture에 포함합니다.
각 쌍이 다른 종류라고 명시하는 합성 근거를 사용하며, 실제 원문 해석이나 미보존 응답의 실패 원인이 확인됐다는 뜻은
아닙니다. 실제로 어떤 대상에 연결됐는지와 그 연결의 의미상 타당성은 별도 읽기 전용 감사 결과로 판단합니다.

복구 호출은 `compare_batch(..., max_attempts_override=1, preserve_source_paths=True)`를 사용합니다.
이 옵션은 ADD의 proposed 경로를 원본 source 경로로 제한하고 모든 root 이동을 금지합니다.
UPDATE/MERGE는 선택한 실제 기존 경로를 사용합니다. 기존 경로와의 충돌과 scope 검증도 그대로 적용합니다.
복구 뒤에도 source coverage와 전체 완료 경계를 pipeline/Backend에서 검증해야 하며, validator 자체가
부분 성공을 저장하지 않습니다.

`world_setting_batch_recovery.py`는 순차 자동 분석의 다중 후보 응답 실패에만 적용합니다. 원본
scope/name이 같은 source를 함께 재생성하고, 기존 속성의 읽기·쓰기 또는 root/scope 구조가 겹치는
성공 그룹은 합쳐 다시 비교합니다. 모든 요청은 같은 고정 targets를 사용하고 마지막 성공 결과
전체를 재검증합니다. 실패한 응답의 일부 decision을 바로 저장하지 않습니다. 독립적인 정상
decision과 typed failures는 원본 source를 정확히 한 번씩 포함한 한 완료 요청으로 보냅니다.

추가 복구 호출은 batch당 최대 20회이며 fresh-context 재시도에도 남은 예산을 공유합니다.
기존 응답 시도와 별도인 이 예산에는 의존 그룹 재비교도 포함됩니다. 상한에 걸린 그룹은
`RECOVERY_CALL_LIMIT`로 남기며 임의로 한 결과를 선택하지 않습니다. 입력·quota·lease·Spring API·
예상 밖 오류는 복구 완료로 바꾸지 않습니다. 완료 진단은 실제 선택한 입력 경로만 API의
`targetWorldSettingId`/`provisionalSubjectKey`로 변환하고, Java는 후보별 최근 30개를 보존합니다.

singleton decision도 batch 완료에 포함되며 별도 단건 recompare로 다시 실행하지 않습니다. batch API를 지원하지
않는 legacy Spring client는 기존 `claim_next_world_setting_comparison` 경로로 후보별 처리하므로 batch
cluster/coverage metric 없이 legacy stale 재시도·실패 격리를 유지합니다.

정확한 `WORLD_SETTING_CANDIDATE_COMPARISON_CONTEXT_STALE` 409는 전체 batch의 context·LLM 결과를 다시 만들어
최대 3회 재시도합니다. `WORLD_SETTING_SUBJECT_RESOLUTION_STALE` 409는 reset endpoint로 기존 batch를 닫고
주체 해소부터 다시 수행한 뒤 새 batch를 claim합니다. 다른 409/4xx/5xx는 batch 실패로 보고합니다. quota가
`AI_TOKEN_QUOTA_EXHAUSTED`이면 현재 batch를 실패 보고하고 다음 batch를 claim하지 않은 채 Job 경계로 전파합니다.

문맥·대상 상한을 넘는 oversized cluster는 Backend가 `REVIEW_REQUIRED`로 처리하고 자체 count metric을 냅니다.
출력은 단건 비교 3000·세계관 batch 비교 16000 token 상한을 사용합니다. batch 호출 전에
모든 source 값·실제 target 경로·decision JSON overhead·안전 여유를 tokenizer로 계산하고, 최소 예상 출력이
16000을 넘으면 provider를 호출하지 않고 후보별 `BATCH_LIMIT_EXCEEDED` 검토 decision으로
전환해 원문과 provenance를 보존합니다.
현재 Python pipeline은 그 결과를 별도 응답으로 받지 않으므로 `clusterOverflowOrReviewRequiredCount`는 Backend
count가 아니며 0으로 추정하지 않고 `null`로 보고합니다. 한 provider 요청에서 여러 decision을 만든 경우
cluster usage는 source 후보 수 비율로 정수 배분하고 `PROPORTIONAL_SHARED_BATCH_REQUEST`라고 명시합니다.
cluster usage 합계는 batch 관측값과 같지만 decision마다 독립 호출했다는 뜻은 아닙니다.
- `json_response.py`
  - 세계관 추출·대상 선택·비교가 공유하는 JSON 객체 파싱, Pydantic 검증, 제한 재시도를 담당합니다.
- `schemas.py`
  - LLM wire 응답과 저장 직전 설정 후보를 각각 검증하는 Pydantic schema를 정의합니다.
  - FastAPI 응답 DTO가 아니라, 외부 LLM 출력이 저장 가능한 구조인지 확인하는 경계 객체입니다.
  - `SETTING`/`CHARACTER_DISCOVERY`와 `SETTING`의 value type을 discriminator로 나눠 필수·null 전용 필드를 Provider JSON Schema에도 노출합니다.
  - strict schema에서 임의 object key를 열지 않기 위해 부가 구조는 검증된 `extra_json` object 문자열로 받고 내부 `value_json` dict로 복원합니다.
  - `NUMBER`/`BOOLEAN`은 wire schema와 저장 경계 모두에서 `value_json.value`가 각각 JSON number/boolean인지 검증합니다.
- `exceptions.py`
  - Analysis 내부 흐름에서만 사용하는 예외를 정의합니다.
  - FastAPI 응답용 공통 예외와 분리해 Worker가 분석 실패 사유를 구분할 수 있게 합니다.

## 실패 메시지 처리

LLM 응답 JSON 객체 파싱은 `json_response.py`의 helper를 사용합니다. 캐릭터 추출은 Provider 응답에 원문 기반 값이 들어 있으므로 자체 retry loop에서 실패를 `reasonCode + fieldLocs`로만 축약합니다. 로그와 다음 prompt에는 Provider 응답 원문·실제 필드 값·검증 메시지를 넣지 않으며, `analysis_job_id`, `source_chunk_id`, attempt, 같은 reason 반복 횟수만 기록합니다.

## 재시도 기준

`CharacterSettingExtractor`의 일반 검증 재시도는 LLM 응답이 JSON으로 파싱되지 않거나, Provider wire model 또는 저장 경계 model의 Pydantic 검증에 실패한 경우에만 수행합니다. 다음 시도는 항상 최초 prompt에 안전한 `reasonCode + fieldLocs`만 붙여 만들며 이전 실패 응답은 재주입하지 않습니다. 후보 하나라도 검증에 실패하면 전체 응답을 재시도하고 일부 후보만 저장하지 않습니다.
캐릭터 설정 추출은 최초 `max_output_tokens=6000`을 사용하고 출력 절단 시 12000으로 한 번만 확장합니다. 세계관 추출도 5000에서 시작해 절단 시 10000으로 한 번만 확장하며, 이 확장은 JSON/schema 검증 재시도 횟수를 소비하지 않습니다.

캐릭터 Fact 비교와 세계관 추출·대상 선택·비교도 JSON/schema/참조 검증 실패만 설정된 횟수만큼 재시도합니다. 캐릭터 비교는 canonical slot과 STATUS 제거·시간 범위 불변식을, 세계관 대상 선택은 입력 ref의 중복·누락 범위를, 세계관 비교는 operation별 target/property와 제안 문자열을 Python에서 추가 검증합니다. DB 문맥 충돌은 LLM 응답 오류와 별도로 각 pipeline이 최신 문맥을 다시 받아 최대 3회 처리합니다.

예를 들어 다음 경우는 재시도 대상입니다.

- JSON 문법이 깨진 응답
- 필수 필드 누락
- `value_type` enum 범위 밖 값
- `confidence`가 0~1 범위를 벗어난 값
- `SETTING`인데 `attribute_name`, `value_type`, `value_json`이 없거나, `CHARACTER_DISCOVERY`인데 설정 값 필드가 채워진 값
- `NUMBER`의 `value_json.value`가 문자열이거나 `BOOLEAN`의 `value_json.value`가 JSON boolean이 아닌 값

반대로 scalar JSON 타입처럼 명시적으로 강제한 계약이 아닌 프롬프트 정책 위반은, schema상 문자열로 유효하면 현재 재시도하지 않습니다.

`source_chunk_id`는 LLM이 생성할 필드가 아니라 호출자가 이미 알고 있는 `EpisodeChunk.id`입니다. 따라서 Provider strict schema에서 제외하고 wire 검증을 통과한 후보에 Worker 입력 ID를 결합한 뒤 저장 경계 schema로 다시 검증합니다.

예를 들어 다음 값은 현재 schema 검증만으로는 통과할 수 있습니다.

- `attribute_name: "item"`
- `attribute_name: "status"`
- `attribute_name: "time. 이름 부여"`
- `attribute_name: "skill.리더십"`
- `confidence: 0.0`

여기서 `time. 이름 부여`는 운영 프롬프트가 지원하는 설정 유형이 아니라, `attribute_name`의 Pydantic shape 검증만으로는 프롬프트 정책 위반 문자열을 차단하지 못한다는 예시입니다. 운영 프롬프트는 시간·사건·타임라인 정보와 제공된 schema에 대응하지 않는 설정을 추출 대상에서 제외합니다.

이런 정책 위반을 Python에서도 강제로 거절하거나 후보 제외 조건으로 만들려면 `ExtractedSettingCandidate`에 attribute 규칙 validator를 추가하거나, schema 검증 이후 별도 policy validation 단계를 둡니다.

## 설정 후보 중복 제거 정책

프롬프트는 같은 청크에서 동일 주체·설정 key·구조화 값을 근거 문장마다 반복 반환하지 않고 가장 명확한 근거 하나만 고르도록 요구합니다. 청크별 LLM 호출은 다른 청크의 결과를 알 수 없으므로, 저장 직전 `SettingCandidateService`가 같은 분석 작업 전체를 한 번 더 중복 제거합니다.

중복 key는 기존 캐릭터와 매칭되었으면 캐릭터 ID, 아니면 정규화한 구체 `entity_name`을 주체로 사용하고, 여기에 `attribute_name`, `value_type`, key 순서를 정규화한 `value_json`을 결합합니다. `attribute_value`는 표시 문구이므로 중복 판정에 사용하지 않습니다. 중복이면 confidence가 더 높은 후보 하나를 남기고, 같으면 먼저 나온 근거를 유지합니다.

동일 `attribute_name`이라도 `value_json`이 다르면 실제 값 변경일 수 있어 모두 유지합니다. `AMBIGUOUS` 주체는 같은 `미상` 문자열이어도 서로 다른 인물일 수 있으므로 중복 제거하지 않습니다.

세계관 후보는 캐릭터 후보와 달리 작가가 확정할 최종 설정 key가 검토 단위입니다. 모든 chunk 추출을 모은 뒤 정규화한 `category + subject_name + setting_name`이 같으면 후보 하나로 통합하고, 서로 다른 추출값은 줄 단위 원본 목록으로, `evidence_spans`와 raw extraction payload는 합집합으로 보존합니다. 2차 비교는 값 하나를 `SINGLE`, 양립 가능한 여러 값을 `MERGED`, 동시에 참일 수 없는 여러 값을 `CONFLICT`로 판정합니다. `MERGED`는 중복을 제거한 자연스러운 최종 문자열을 제안하지만 `CONFLICT`는 추출값 목록을 바꾸지 않고 사용자가 최종값을 정하도록 남깁니다.

## 캐릭터명 매칭 정책

LLM은 기존 캐릭터 DB와의 확정 매칭을 하지 않습니다. LLM은 원문에 실제 나온 표현인 `raw_entity_mention`과 원문 맥락에서 정리한 표시 후보명인 `entity_name`만 반환합니다.

저장 직전 Python resolver가 Spring claim payload의 `knownCharacters`를 받아 다음 순서로 매칭합니다.

`candidate_kind=CHARACTER_DISCOVERY`는 `entity_name` 자체가 발견한 이름이라는 별도 계약을 사용합니다. 따라서 `raw_entity_mention="케닉의 넷째 아들 세룸"`에 기존 캐릭터 `케닉`이 포함돼도 케닉으로 연결하지 않고, `entity_name="세룸"`과 기존 이름 목록만 비교합니다. 기존 이름과 매칭되면 발견 후보를 저장하지 않고, 매칭되지 않으면 `UNRESOLVED` 검토 후보로 저장합니다. 발견 후보는 subject fallback 대상이 아닙니다.

일반 `SETTING` 후보에서도 등록되지 않은 구체 `entity_name`이 `raw_entity_mention` 안에 직접 등장하면, 같은 표현에 함께 나온 기존 관계자 이름보다 새 주체명을 우선해 `UNRESOLVED`로 남깁니다.

```text
raw_entity_mention 정규화
entity_name 정규화
knownCharacters 이름을 한 번 정규화
-> raw match 후보 계산
-> entity match 후보 계산
-> 아래 우선순위로 match_status 결정
```

`raw_entity_mention`은 원문에 실제 등장한 표현이므로 우선권을 갖습니다. `entity_name`은 LLM이 같은 청크 문맥에서 정리한 후보명이므로, raw가 명확하지 않거나 충돌 여부를 확인할 때 보조로 사용합니다.

| 상황 | 결과 | 이유 |
| --- | --- | --- |
| `raw_entity_mention`이 `나`, `내 캐릭터`, `주인공`, `그`, `그녀` 같은 지칭어 + entity가 기존 캐릭터 1명과 매칭 | `MATCHED` | 같은 청크에서 LLM이 구체화한 후보명이 기존 캐릭터 하나와 유일하게 연결되면 문맥 추론을 살림 |
| `raw_entity_mention`이 지칭어 + entity가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | LLM 정리명만으로도 하나를 고를 수 없음 |
| entity가 없거나 `미상`/지칭어 같은 구체적이지 않은 값 | LLM subject fallback 대상 | raw 표현의 형태와 관계없이 previous/current/next chunk 문맥으로 주체만 해소한 뒤 일반 매칭 로직으로 진행 |
| `raw_entity_mention`이 지칭어 + entity가 기존 캐릭터와 매칭 실패 | `UNRESOLVED` | 기존 캐릭터와 연결할 근거는 없지만 신규 캐릭터 후보일 수 있음 |
| subject fallback 정상 응답에서도 주체를 해소하지 못함 | `AMBIGUOUS` | 후보의 설정과 근거는 보존하고 사용자가 캐릭터 연결을 판단하도록 `entity_name="미상"`으로 정규화 |
| raw가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | 어느 캐릭터인지 하나로 확정할 수 없음 |
| raw가 기존 캐릭터 1명과 매칭 + entity가 다른 기존 캐릭터 1명과 매칭 | `AMBIGUOUS` | 원문 표현과 LLM 정리명이 서로 다른 캐릭터를 가리키는 충돌 |
| raw가 기존 캐릭터 1명과 매칭 + entity가 없거나 같은 캐릭터와 매칭 | `MATCHED` | 원문 표현을 우선해 `matched_character_id`를 채움 |
| raw는 매칭 실패 + entity가 기존 캐릭터 여러 명과 매칭 | `AMBIGUOUS` | LLM 정리명만으로도 하나를 고를 수 없음 |
| raw는 매칭 실패 + entity가 기존 캐릭터 1명과 매칭 | `MATCHED` | 원문 표현은 설명형이거나 지칭어일 수 있지만 LLM 정리명이 한 명과만 연결됨 |
| raw와 entity 모두 기존 캐릭터와 매칭 실패 | `UNRESOLVED` | 기존 캐릭터와 연결할 근거가 없음. 신규 캐릭터 후보일 수 있음 |

매칭 방식은 완전 일치를 먼저 보고, 이후 한쪽 이름이 다른 쪽에 포함되는 경우를 확인합니다. 단, 한 글자 이름/표현은 오탐이 많으므로 포함 관계 매칭에서 제외합니다.

### adjacent chunk subject fallback

`entity_name`이 비어 있거나 `미상`, `불명`, `나`, `그녀`, `주인공`처럼 구체적인 캐릭터명이 아닌 후보는 current chunk만으로 주체가 풀리지 않은 상태입니다. `raw_entity_mention`은 fallback 판단에 사용할 입력이지만, 그 값이 미리 정한 지칭어 목록에 들어가는지를 fallback 진입 조건으로 사용하지 않습니다.

이 경우 단순히 주변 청크에서 기존 캐릭터 이름을 문자열로 찾지 않습니다. 주변에 이름이 등장한다는 사실만으로 지칭 대상을 확정하면 잘못된 캐릭터 설정이 저장될 수 있기 때문입니다.

현재 구현은 fallback 대상 후보를 current chunk 기준으로 묶고, previous/current/next chunk 문맥과 함께 LLM subject resolver에 전달합니다.

fallback 진입/처리 기준:

| 상황 | fallback 호출 | 처리 |
| --- | --- | --- |
| raw가 지칭어이고 entity가 기존 캐릭터 1명과 매칭 | 호출하지 않음 | 기존 매칭 로직에서 `MATCHED` |
| raw가 지칭어이고 entity가 기존 캐릭터 여러 명과 매칭 | 호출하지 않음 | 기존 매칭 로직에서 `AMBIGUOUS` |
| raw가 지칭어이고 entity가 기존 캐릭터와 매칭 실패 | 호출하지 않음 | 신규 캐릭터 가능성이 있으므로 `UNRESOLVED` |
| entity가 없거나 `미상`/지칭어 같은 구체적이지 않은 값 | 호출함 | raw가 없거나 예상하지 못한 원문 표현이어도 previous/current/next chunk로 주체를 재판단 |
| fallback 응답의 `resolved_entity_name`이 구체 이름 | - | candidate의 `entity_name`만 치환하고 기존 매칭 로직으로 진행 |
| fallback 응답의 `resolved_entity_name`이 null | - | 원래 후보를 보존하고 `entity_name="미상"`으로 정규화한 뒤 기존 매칭 로직에서 `AMBIGUOUS` 처리 |
| fallback 응답의 `resolved_entity_name`이 `미상`, `그녀`, `주인공` 같은 placeholder/지칭어 | - | null과 같은 정상적인 해소 실패로 보고 후보를 `미상`으로 보존 |
| 응답 JSON/schema가 잘못되거나 candidate ID가 누락·중복·추가됨 | - | 사용자 판단 대상이 아닌 기술적 계약 오류이므로 분석 실패로 전파 |

```text
raw_entity_mention이 지칭어 + entity_name이 기존 캐릭터 1명과 매칭
-> MATCHED

raw_entity_mention이 지칭어 + entity_name이 기존 캐릭터 여러 명과 매칭
-> AMBIGUOUS

entity_name이 "미상" 또는 지칭어 같은 구체적이지 않은 값
-> 같은 current chunk의 fallback 대상 후보를 batch로 묶음
-> previous/current/next chunk와 knownCharacters를 LLM subject resolver에 전달
-> resolved_entity_name이 구체 캐릭터명이면 entity_name만 치환한 뒤 일반 매칭 로직으로 진행
-> resolved_entity_name이 null, "미상", "그녀" 같은 placeholder/지칭어이면 entity_name을 "미상"으로 정규화
-> character_name_resolver가 AMBIGUOUS로 계산해 사용자 검토 후보로 저장

raw_entity_mention이 지칭어 + entity_name이 기존 캐릭터와 매칭 실패
-> UNRESOLVED

raw_entity_mention이 지칭어 + entity_name이 이미 구체 후보명
-> fallback을 호출하지 않고 entity_name 기준 매칭 정책으로 진행
```

fallback은 설정 후보 추출을 다시 하는 단계가 아니라, 이미 추출된 후보의 주체만 해소하는 좁은 resolver입니다. previous/next chunk는 판단 문맥으로만 사용하고, `source_chunk_id`, `evidence_spans`, offset 기준은 후보가 실제 추출된 current chunk를 유지합니다.

LLM subject resolver는 `MATCHED`, `UNRESOLVED`, `AMBIGUOUS` 같은 최종 매칭 상태를 판단하지 않습니다. LLM이 확실한 주체명만 `resolved_entity_name`으로 반환하면 Python이 후보의 `entity_name`만 치환하고, 이후 기존 `character_name_resolver`가 `knownCharacters`와 비교해 최종 `matched_character_id`, `match_status`를 계산합니다.

`resolved_entity_name`에는 `미상`, `불명`, `unknown`, `나`, `그`, `그녀`, `주인공` 같은 placeholder/지칭어가 들어오면 안 됩니다. LLM이 정상 응답에서 null 또는 이런 값을 반환하면 Python은 실제 해소 실패로 보고 원래 후보를 `entity_name="미상"`으로 보존합니다. 이후 기존 `character_name_resolver`가 이를 `AMBIGUOUS`로 계산하므로 `UNRESOLVED`의 새 캐릭터 후보로 잘못 표시되지 않습니다.

응답 파싱/schema 검증 실패와 candidate ID 누락·중복·추가는 의미상 해소 실패가 아니라 외부 응답 계약 위반입니다. 이런 기술적 실패는 `AMBIGUOUS`로 숨기지 않고 분석 실패로 전파합니다.

예시 입력:

```json
{
  "known_characters": [
    {
      "character_id": "00000000-0000-0000-0000-000000000101",
      "name": "비요른 얀델"
    }
  ],
  "context": {
    "previous_chunk": "비요른 얀델은 낡은 도끼를 들고 있었다.",
    "current_chunk": "나는 1레벨 바바리안으로 깨어났다.",
    "next_chunk": "주변에는 다른 인물이 없었다."
  },
  "candidates": [
    {
      "candidate_id": "candidate-0",
      "raw_entity_mention": "나는",
      "entity_name": "미상",
      "attribute_name": "level",
      "attribute_value": "1",
      "evidence_quotes": ["나는 1레벨 바바리안으로 깨어났다."]
    }
  ]
}
```

예시 응답:

```json
{
  "resolutions": [
    {
      "candidate_id": "candidate-0",
      "resolved_entity_name": "비요른 얀델",
      "reason": "앞뒤 문맥에서 1인칭 서술 주체가 비요른 얀델로 이어진다."
    }
  ]
}
```

처리 결과:

```text
candidate-0.entity_name = "비요른 얀델"로 치환
attribute/value/evidence/source_chunk는 유지
character_name_resolver가 기존 캐릭터 목록과 비교해 MATCHED / UNRESOLVED / AMBIGUOUS 계산
```

해소할 수 없는 경우:

```json
{
  "resolutions": [
    {
      "candidate_id": "candidate-0",
      "resolved_entity_name": null,
      "reason": "앞뒤 문맥만으로 주체를 특정할 수 없다."
    }
  ]
}
```

이 경우 원래 후보의 설정값, 근거, source chunk를 유지하고 `entity_name`만 `"미상"`으로 정규화합니다. LLM이 `resolved_entity_name`에 `"미상"` 또는 `"그녀"` 같은 문자열을 넣어도 같은 방식으로 보존하며, 저장 단계의 기존 캐릭터명 매칭 로직이 최종 상태를 `AMBIGUOUS`로 계산합니다.

### subject fallback trace 정책

현재 저장/출력 구조에서는 fallback 전체 개수만 summary로 확인할 수 있습니다.

```text
subjectFallbackCallCount
subjectFallbackResolvedCount
subjectFallbackUnresolvedCount
```

`subjectFallbackUnresolvedCount`는 subject resolver가 정상 응답을 반환했지만 구체 이름을 찾지 못해 `미상`으로 보존한 후보 수입니다. 최종 `AMBIGUOUS` 상태는 이후 기존 캐릭터명 매칭 단계에서 계산하므로, subject resolver 내부 지표에는 `Ambiguous` 대신 `Unresolved`를 사용합니다.

최종 `settingCandidates[]`에서는 해소 실패 후보가 `미상 + AMBIGUOUS`로 보존된 사실을 볼 수 있습니다. 다만 어떤 chunk에서 fallback이 호출됐는지, LLM이 null을 반환한 이유가 무엇인지, 원래 `entity_name`이 무엇이었는지는 별도 trace 없이는 알 수 없습니다.

후보별 fallback 이력을 확인하려면 별도 trace 구조가 필요합니다.

예시:

```json
{
  "chunk_index": 7,
  "source_chunk_id": "chunk-id",
  "candidate_id": "candidate-0",
  "raw_entity_mention": "나는",
  "original_entity_name": "미상",
  "resolved_entity_name": "비요른 얀델",
  "result": "RESOLVED",
  "unresolved_reason": null
}
```

다만 이 trace를 어디까지 남길지는 정책 결정이 필요합니다.

| 선택지 | 장점 | 주의점 |
| --- | --- | --- |
| debug runner JSON에만 남김 | 로컬 검증과 PR 리뷰에 충분하고 DB 영향이 없음 | 운영 이력으로는 조회할 수 없음 |
| Worker summary JSON에 요약/샘플만 남김 | 분석 job 단위 관측성이 생김 | summary가 커질 수 있어 개수 제한 정책 필요 |
| `setting_candidates.raw_ai_result_json`에 후보별 trace를 남김 | 저장된 후보와 fallback 이력을 함께 볼 수 있음 | 현재 값에는 fallback 응답과 판단 사유가 포함되지 않으므로 별도 구조가 필요 |
| 별도 로그/실패 이력 테이블에 남김 | 운영 디버깅에 가장 강함 | 스키마와 보존 기간 정책이 필요 |

현재 구현은 trace를 저장하지 않고 count만 남깁니다. 후보별 fallback 위치와 해소 실패 사유를 제품/운영에서 조회해야 한다면, 위 선택지 중 하나를 정한 뒤 debug 출력, Worker summary, DB 저장 범위를 함께 조정합니다.

`subjectFallbackUnresolvedCount`에는 LLM fallback 정상 응답으로도 구체 이름을 찾지 못해 `미상`으로 보존된 후보만 포함됩니다. malformed 응답이나 candidate ID 계약 위반은 분석 실패이므로 이 개수에 포함하지 않습니다.

## 후속 작업

- NVM-143에서 설정 후보와 기존 fact, 직접 근거, pgvector Top-K 결과를 조합합니다.
- NVM-144에서 NVM-143이 모은 검증 문맥을 기준으로 최종 충돌 여부를 판정합니다.
- 설정 추출 재시도와 subject fallback을 포함한 LLM token usage를 Worker 단위로 집계해 Spring 완료 보고에 연결합니다.
- 프롬프트 정책 위반 후보를 schema validator, 후처리 필터, LLM 재시도 중 어디에서 다룰지 결정합니다.
- subject fallback의 prompt 품질과 호출 단위가 충분한지 실제 원문으로 검증합니다.
- fallback에서 해소된 후보와 `미상`으로 보존된 후보의 trace를 debug JSON, Worker summary, DB 중 어디에 남길지 정책을 결정합니다.
