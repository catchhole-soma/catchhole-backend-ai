# 회차별 인물 관계도 계약 v1 (GH323)

## 제품 의미와 범위

관계도는 선택한 회차까지 독자가 알게 된 관계를 보여준다. 사건이 실제로 발생한 과거 시점의 복원이 아니다. 5화에서 새로 드러난 관계는 1화 화면에 소급하지 않는다. 서로의 감정, 함께 행동하는 관계, 사회적 관계를 구분하며 방향이 다른 관계를 같은 선의 내용으로 합치지 않는다. 종료된 관계도 상태 `ENDED`로 남고 화면은 점선과 종료 표시를 사용한다.

가계도는 기존 규칙 기반 가족 도메인과 API를 그대로 사용한다. 기존 캐릭터/세계관 프롬프트·비교·자동 반영 contributor 목록은 변경하지 않는다. 관계 추출과 관계 비교만 독립 호출하며 `RELATIONSHIP_EXTRACTION`, `RELATIONSHIP_COMPARISON` 토큰 목적을 사용한다. 제공자 호출의 권한·quota·lease 검사는 기존과 동일하다.

불분명한 관측은 내부 `HOLD`로 보관하고 검토함을 만들지 않는다. 검증되지 않은 관측이 화면에서 확정 관계인 것처럼 나타나지 않는다. 관련 인물이 새 근거에 다시 등장하면 과거 HOLD를 비교에 제공하며, 새 관측과 함께 해결한 HOLD는 다시 제시하지 않는다. 다만 이름이 없는 1인칭/대명사를 실제 캐릭터와 안전하게 연결하는 별도 근거는 현재 없다. `name=null`인 관측을 주인공이라고 추정해 연결하거나 현재 프로필 이름으로 치환하지 않으며, 이 보류가 자동으로 모두 해결된다고 보장하지 않는다.

이름이 원문에 확인되지만 프로필이 없는 인물은 독립 노드가 될 수 있다. 모델에 DB UUID를 보내지 않는다. 노드 이름은 관계 분석 시점의 이름을 보존한다. 캐릭터 등록 여부도 연결된 회차 이후에만 true다. 프론트는 현재 캐릭터/프로필/사진 API를 호출해 과거 관계도에 덧씌우지 않는다. 짧은 이름/전체 이름을 자동으로 합치는 별칭 해소는 하지 않는다. 서로 다른 노드가 될 수 있다. 동일 이름·대명사의 어려운 해소는 보수적으로 보류하며 이 기능은 캐릭터 추출의 identity 정책을 대체하지 않는다.

기존 미등록 노드도 뒤 회차의 관계 반영 때 유일하게 확인된 프로필에 연결한다. 이번 작업의 확정 후보와 prepare 당시 고정된 인물 ID를 사용하며, 동명 노드가 여럿이거나 두 근거의 ID가 다르면 연결하지 않는다. 최초 등장 회차는 유지하고 프로필 연결 회차는 이번 회차로 기록하므로, 1화에서 미등록이었던 인물이 2화에 연결돼도 1화는 미등록으로 남는다. 관계 반영 후 수동으로 확정한 프로필은 다음 관계 분석의 고정 입력으로 연결하며, 화면 조회 중 현재 프로필 DB를 읽어 과거 관계도를 갱신하지 않는다.

수동 분석에서 뒤 회차의 prepare가 먼저 실행돼도, 앞 회차 원문에 같은 정확한 이름이 확인되고 작품 전체의 해당 노드가 하나라면 기존 ID를 재사용한다. 최초 관측 회차만 앞당기고 프로필 연결 회차는 유지한다. 현재 고정 인물 ID와 충돌하거나 동명 노드가 여러 개면 추측 병합하거나 새 중복 노드를 만들지 않고 보류한다. 이번 원문에 없는 미래 이름은 비교 문맥에 넣지 않으며, 기존 관계 ID·사용자 정정·삭제 억제와 후행 문맥 무효화 정책은 유지한다. 이미 서로 다른 UUID로 저장된 동명 노드를 자동 합치는 데이터 보정은 하지 않는다.

## 활성화와 작업 수명

`RELATIONSHIP_GRAPH_ENABLED`(기본 true)는 신규 claim의 기능 활성화를 제어한다. Worker는 claim에 `supportsRelationshipAnalysis: true`를 광고해야 한다. 서버는 활성화한 EPISODE/SETTING_EXTRACTION Job에만 `relationshipAnalysisContract: {formatVersion: 1}`을 반환한다. 메타데이터가 없으면 Worker는 관계 API를 호출하지 않는다. 설정집/비교 전용 작업에는 기능을 붙이지 않는다. 한 번 활성화된 Job 재시도는 지원 Worker만 claim할 수 있다.

관계 상태는 기존 checkpoint와 별개로 `PENDING → PREPARED → COMPLETED → APPLIED`를 사용한다. 추출/비교 의미 처리 실패는 `FAILED`, 입력의 재분석·파기로 무효가 된 결과는 `INVALIDATED`다. PREPARED에는 관측, 비교 문맥, 실제 ID 매핑이 저장되며 재시작은 재추출하지 않는다. COMPLETED에는 검증된 결정이 저장되고 기존 핵심 설정 반영 후 같은 트랜잭션에서 APPLIED가 된다. PENDING/PREPARED인 채 원 Job을 완료할 수 없다. 관계가 FAILED이면 핵심 설정 완료를 허용한다.

인증, lease, quota, 원문 충돌, 저장 장애를 의미 처리 실패로 바꿔 우회하지 않는다. 원문 S3 읽기는 DB 트랜잭션 밖에서 실행하고 읽기 전후 lease와 고정 manifest를 확인한다. 내부 변경은 Work→Job 순서로 잠근다.

ORDERED_PROVISIONAL은 기존 회차 순서를 따른다. CONFIRMED_ONLY/수동 작업이 병렬 실행되는 경우에도 prepare가 읽은 앞 회차 상태와 사용자 수정·삭제 상태를 동결한다. complete/finalize 때 의존성이 바뀌었으면 409로 거절한다. 뒤 회차가 먼저 완료됐더라도 앞 회차가 반영되면 후행 관계 결과를 무효화한다. 조용히 이전 문맥으로 합치지 않는다. 의존성이 바뀐 PREPARED/COMPLETED는 GET 재개와 같은 prepare 재전송부터 409로 거절해 유료 비교를 다시 호출하지 않게 한다. 같은 Job의 자동 재시도로 문맥을 바꾸지 않으며 새 분석 Job이 필요하다.

같은 회차를 다시 분석하면 기존 해당 회차와 후행 관계 결과는 INVALIDATED가 된다. 원문 hash뿐 아니라 key/version/회차 번호·보관·파기 여부를 검사한다. 원문이 바뀌었거나 INVALIDATED인 최초 회차 뒤의 결과도 공개하지 않는다. 다시 유효하게 만들려면 해당 회차부터 순서대로 분석해야 한다. 일반 분석은 무효화된 후행 회차를 자동 유료 재분석하거나 과거 모든 회차를 백필하지 않는다. GH332의 고정 선정 계정은 별도 관계 전용 캠페인에서만 과거 회차를 순서대로 백필한다([실행 계약](relationship-cohort-backfill.md)). 이전 성공 회차를 표시하는 경우에도 `analysisStatus`와 `partial`로 누락 상태를 함께 반환한다.

## 내부 Worker API

공통 인증: `X-Internal-Api-Key`, `X-Worker-Lease-Token`. 응답은 기존 `CommonResponse.data` 봉투다.

| 메서드 | 경로 | 의미 |
|---|---|---|
| GET | `/api/internal/v1/analysis-jobs/{jobId}/relationship-analysis` | PENDING/미저장은 204. PREPARED/COMPLETED/FAILED는 저장한 Context. sourceHash 검증 후 재개 |
| POST | 위 경로 + `/prepare` | 원문 인용 검증 후 관계 비교 입력 동결; 동일 요청 재전송은 동일 Context |
| POST | 위 경로 + `/complete` | contextToken과 결정 검증 후 저장. 동일 완료 요청은 멱등; 다른 결과로 덮어쓰기 금지 |
| POST | 위 경로 + `/fail` | 관계 의미 처리 실패 기록. prepare 전 contextToken은 null |

Java DTO 단일 출처는 `RelationshipGraphContract`이며 OpenAPI는 실제 MVC `/v3/api-docs`에서 생성한다.

```json
{"formatVersion":1,"sourceHash":"<sha256>","observations":[{"ref":"O1","source":{"name":"아라","mention":"아라"},"target":{"name":"보라","mention":"보라"},"summary":"함께 여행한다","evidence":[{"startOffset":0,"endOffset":18,"quote":"아라와 보라는 함께 길을 떠났다."}]}],"rejectedObservationCount":0}
```

위 인용 offset은 예시 문자열의 실제 Unicode code point 길이에 맞춰 계산해야 한다. 실제 입력은 SHA-256과 quote/offset이 원문과 정확히 일치해야 한다. NFC 정규화, 공백 제거, 줄바꿈 변경을 하지 않는다. start 포함/end 제외이며 회차 전체 기준이다. Java UTF-16 char index가 아니다.

Context는 `formatVersion, sourceHash, contextToken, status, sourceEpisodeNo, observations, nodes, relationships, heldObservations`를 가진다. `N*`는 인물 참조, `R*`는 기존 관계, `O*`는 이번 관측, `H*`는 관련 과거 보류다. 관측에는 `sourceNodeRefs,targetNodeRefs,sourceEpisodeNo`가 추가된다. 관계에는 `knownFromEpisodeNo,lastChangedEpisodeNo,status`가 있어 미래 관계가 섞이지 않는다. 모델에 보내는 nodes는 실제 O/R/H가 참조하는 범위만 포함한다.

완료는 `{formatVersion:1,contextToken,decisions:[...]}`. 결정 필드는 `observationRefs,operation,relationshipRef,sourceNodeRef,targetNodeRef,axis,kind,label,summary,direction,temporalScope,reason`이다.

| 작업 | 허용 내용 |
|---|---|
| ADD | 기존 slot이 없는 관계 추가. relationshipRef=null |
| MERGE | ACTIVE 관계의 새 근거 추가. label/summary/방향/주체/종류는 기존과 동일 |
| UPDATE | 설명/label 갱신 또는 ENDED 관계 재활성화. 기존 slot 참조 |
| END | ACTIVE 관계 종료. 과거 근거/상태 보존 |
| EXCLUDE | 관계로 사용하지 않는 관측. reason 필수, 관계/노드/type/value 필드는 null |
| HOLD | 내부 보류. EXCLUDE와 같은 null 규칙; 공개 관계 생성 없음 |

slot은 두 노드 + axis + kind + direction이다. DIRECTED는 순서를 보존하고 MUTUAL/UNDIRECTED는 대칭이다. 한 관측으로 친구 END와 적대 ADD처럼 서로 다른 slot을 바꿀 수 있다. 같은 slot을 두 번 쓰거나 같은 관측을 HOLD/EXCLUDE와 쓰기 결정 양쪽에 넣을 수 없다. 신규 O는 모두 한 번 이상 처리해야 하며 H만으로 과거 관측을 재해석하지 않는다. H를 쓸 때도 신규 O가 필요하다. 각 관측의 인물 후보가 양쪽 모두 하나일 때만 쓰기를 허용한다. 기존 R을 사용할 때는 저장된 source/target 순서를 그대로 사용한다.

`axis`: SOCIAL, ATTITUDE, INTERACTION.
`kind`: FRIENDSHIP, COMPANIONSHIP, COOPERATION, MENTORSHIP, ROMANCE, HOSTILITY, TRUST, DISTRUST, AFFECTION, ACQUAINTANCE, OTHER.
`direction`: DIRECTED, MUTUAL, UNDIRECTED.
`temporalScope`: PRESENT, PAST, HYPOTHETICAL, UNKNOWN. PRESENT는 현재 회차에서 새로 드러난 지식이다. PRESENT만 ADD/MERGE/UPDATE/END할 수 있다.

실패 요청은 `{formatVersion:1,sourceHash,contextToken,failureCode,failureDiagnostics?}`다. optional 진단은 공통 v1의 허용 코드·검사·승인된 짧은 구절만 정제해 첫 실패에 보존하며, 구 Worker의 4필드 요청도 허용한다. failureCode는 INVALID_RESPONSE, EXTRACTION_FAILED, COMPARISON_FAILED, INPUT_TOO_LARGE다.

한 prepare 최대 신규 관측 384개, 관측당 인용 1~12개, 한 인용 480 code point 이하, 관측당 인용 총 4000 code point 이하, 이름/mention 100 이하, 관측 summary 300 이하. 관계 설명 summary는 500 이하. 완료 결정 최대 512개, label 60 이하, reason 500 이하. legacy는 한쪽 인물이라도 겹치는 과거 R/H를 포함한다. 새 partial Context는 양쪽 인물이 각각 하나로 해소된 신규 O의 정확한 무방향 쌍에 필요한 R/H만 먼저 투영한다. 미해소·다른 쌍의 H는 저장에 남기고 이 입력에서는 제외한다. 투영 후 관련 H가 384개 또는 비교 문맥의 노드/관계가 각각 1536개를 초과하면 자르지 않고 FAILED/INPUT_TOO_LARGE Context를 저장해 반환한다. `rejectedObservationCount`는 검증 탈락을 성공한 빈 결과로 숨기지 않기 위한 비음수 개수다.

### 기존 단일 완료 계약의 비교 입력 상한과 실패 진단 (GH332)

전체 고정 비교 문맥이 64,000 추정 입력 토큰 이내이면 기존 Terra/medium 단일 호출과 prompt/schema를 그대로 사용한다. 상한을 넘을 때만 O/H의 `sourceNodeRefs × targetNodeRefs`가 만드는 가능한 무방향 인물 쌍을 기준으로 나눈다. 가능한 쌍이 겹치는 관측은 같은 묶음이며, 과거 H가 여러 묶음을 잇거나 H끼리 이어지는 경우도 모두 합친다. 가능한 쌍 전체를 메모리에 생성하지 않고 endpoint 집합의 교집합으로 겹침을 검사한다. 방향·axis·kind가 다른 같은 인물 쌍을 분리하지 않는다. 인물 후보가 없는 관측을 이름 추측으로 해소하지 않는다.

각 묶음에는 해당 쌍의 기존 R 전체, 해당 O/H, 이들이 참조하는 N을 원래 ref·순서·인용·내용 그대로 제공한다. O가 없는 H 묶음은 재해석하지 않으며 저장된 보류도 그대로 남는다. 독립 묶음은 입력 상한 안에서 합쳐 호출 횟수를 줄인다. 모든 묶음의 입력 크기와 전체 최대 16호출을 첫 모델 호출 전에 검사하고, 쪼갤 수 없는 단일 묶음이 초과하면 원문을 자르지 않고 INPUT_TOO_LARGE로 실패한다. 호출은 순차 실행하며 기존 heartbeat·계량·서버 예산 검사를 유지한다. 출력 오류를 재질문하는 repair는 추가하지 않는다.

각 O는 정확히 한 묶음에 배정하지만 기존 계약처럼 그 O를 서로 다른 관계 결정에 여러 번 사용할 수 있다. 묶음별 결과를 모두 받은 뒤 원래 전체 문맥으로 결정 최대 512개·전체 O coverage·참조·시점·중복 슬롯을 다시 검증한다. 검증 전에는 부분 결과를 Spring에 저장하지 않는다. 다른 인물 쌍의 문맥이 줄어들 수 있으므로 이 분할은 상한 초과 경로에만 적용하며, 실제 모델 의미 품질 개선을 자동으로 보장하지 않는다.

진단은 공통 `relationship-failure-diagnostics-v1.json`의 고정 코드만 사용한다. stage는 `RELATIONSHIP_EXTRACTION`/`RELATIONSHIP_COMPARE`이며, JSON/schema·관측 coverage·순서·슬롯·MERGE 내용 변경·인물 해소·입력 상한 등을 구분한다. 전체 원문·전체 모델 응답·예외 원문은 기록하지 않는다. 사용자 승인된 짧은 문제 구절은 GH357의 최대 3개·각 120 code point 제한을 따른다. 관계 pipeline은 원본 실패의 분류와 안전한 진단을 관계 fail API 및 전용 Worker에 전달하고, 상위 runner에는 고정 메시지만 노출한다. 일반 캐릭터/세계관 분석에서 관계 실패를 격리하는 의미는 유지한다.

### 인물 쌍별 부분 완료 (GH359)

`formatVersion=1`을 유지하며 claim의 `supportsRelationshipPartialResults=true`와 서버가
새 Job에 동결한 `relationshipAnalysisContract.supportsPartialResults=true`, Context의
`supportsPartialResults=true`가 모두 있을 때만 다음 추가 계약을 사용한다. 기존 고정 v1
작업은 새 Worker가 재선점해도 승격하지 않는다. 새 복구 Job은 보존된 추출 관측으로 새
Context를 준비하면서 활성화할 수 있다. 구 Worker는 능력이 동결된 Job 및 현재 공개/생성
세대에 유효한 부분 실패가 있는 작품의 후속 관계 작업을 claim하지 않는다.

모델 입력은 인물 쌍별 그룹으로 구성하고 같은 두 인물의 방향·axis·kind는 한 그룹에서 판단한다.
이미 연결된 두 인물의 참조는 모델에게 다시 작성시키지 않는다. 이름이 미확정인 새 O는 내부
HOLD로 보존하고, 미확정 또는 다른 쌍의 H는 모델 입력에서 제외하되 원래 보류를 삭제·소비하지
않는다. 명확하지 않은 지속 관계를 추측하기보다 직접 입증된 상호작용만 표현한다. 별칭·동일성
추정 병합이나 사용자에게 보류 목록을 검토시키는 기능은 추가하지 않는다.

구조/검증 실패는 해당 쌍에 한 번만 제한적으로 다시 묻는다. 참조나 JSON을 읽을 수 없어
영향 범위를 특정할 수 없으면 해당 호출의 그룹 전체가 대상이다. 초기 최대 16 batch와 실패
그룹의 repair 최대 16 batch, 총 최대 32 provider 호출이며 repair 입력 여유도 미리 검사한다.
비교 batch는 출력에도 여유를 둔다. 쌍당512토큰을 계획값으로 잡고 출력의20% 이상을
추론·변동 여유로 남기며 batch당최대24쌍이다. 기본 출력16,000에서는384쌍도16batch에
들어간다. 512는 보장 상한이나 응답 절단 기준이 아니며, 한 쌍의 전체 O/H를 쪼개거나
출력 추정만으로 해당 쌍을 실패시키지 않는다. 실제 출력 절단은 기존 실행 실패로 남는다.
한 쌍의 입력 또는 전체 호출 상한 초과는 `RELATIONSHIP_INPUT_TOO_LARGE`로 보존한다.
최종 512개 결정 상한을 넘기는 쌍은 `RELATIONSHIP_RESPONSE_SCHEMA_INVALID`로 보존한다.
provider HTTP/timeout/output truncation, quota, lease, 권한, 고정 문맥·원문·저장 오류는 이
복구나 부분 완료로 숨기지 않는다. 추가 호출도 기존 예약/정산과 사용자·서비스 부담 정책을 따른다.

완료 요청은 기존 `decisions`와 `failures:[{observationRefs:["O1","O2"],reasonCode:"RELATIONSHIP_ENDPOINTS_UNGROUNDED"}]`를
함께 보낸다. 실패는 양쪽 인물이 유일한 같은 무방향 인물 쌍의 새 O 전체여야 하며, 성공과
실패의 출처는 겹치지 않고 전체 새 O를 덮어야 한다. 정상 결정의 주체·근거·시점·중복 slot·
MERGE 불변 검증은 유지한다. reason은 기존 RELATIONSHIP 진단 중 CONTEXT_INVALID를 제외한
코드와 `RELATIONSHIP_PREDECESSOR_INCOMPLETE`만 허용한다. 임의 오류 문자열은 받지 않는다.

Spring은 정상 결정과 failures를 원자로 저장한다. 부분 계약의 terminal Context는 빈 배열을
포함해 `failures`를 항상 반환하므로 완료 직전 재시작이 실패를 지우거나 provider를 재호출하지
않는다. graph는 APPLIED에서도 failures와 `failure_code=PARTIAL_COMPARISON_FAILED`를
보존한다. 실행 Job의 SUCCEEDED는 결과의 완전 성공이 아니며 전용 회차 항목은 PARTIAL이다.

앞 회차 실패 쌍은 Context의 local N 번호가 아닌 저장된 node UUID로 연결한다. 현재 새 O에
필요한 쌍만 `blockedPairs:[{sourceNodeRef,targetNodeRef,firstFailedEpisodeNo}]`로 동결한다.
이 쌍의 후속 O는 provider에 보내지 않고 PREDECESSOR_INCOMPLETE로 남겨 누락 상태 위에서
UPDATE/END하지 못하게 한다. 독립 쌍은 진행한다. 앞부분 복구는 후행 무효화·순서 재구성이
필요하며 이 변경은 기존 자동 복구 예산을 늘리지 않는다. 원문 추출 전체 실패는 FAILED이고
전용 백필은 BLOCKED다. 거절된 추출 관측처럼 실패 쌍을 모르면 기술 확인 범위를 보수적으로
표시하되 새로운 전역 실행 차단으로 확대하지 않는다.

APPLIED의 새 기술 실패는 V111 trigger가 기존 outbox에 RELATIONSHIP_COMPARISON으로
기록한다. V107의 서버 생성 실행 시도 ID와 발송 묶음을 재사용하고, V109의 검사·발췌 정제는
유지한다. 이 새 사건은 허용 reason별 인물 쌍 개수와 안전한 ID만 저장하며 후보/실패 작업 수와
구분해서 표시한다. 선행 실패의 파급은 반복 알림하지 않는다. 동일 완료 재전송은 같은 completion hash를 요구하며 event·알림을 중복 생성하지 않는다.

공개 Graph의 `updateIncomplete`와 `incompleteEpisodeNos`는 선택 회차/공개 세대의 기술적
누락만 표시한다. 기존 `partial`에는 의미상 HOLD도 포함되므로 기술 실패로 해석하지 않는다.
Edge의 `lastVerifiedEpisodeNo`는 해당 쌍의 기술적 완료가 이어진 마지막 회차다. 변경이 없는
정상 회차에서도 진행하고 첫 쌍 실패 또는 위치 미상의 전체 누락 앞에서 멈춘다. 관계 변경 회차와
다르며 서사 의미의 절대 정확성 보장은 아니다. 0은 확인 가능한 완료 구간이 없다는 뜻이다.
미완료인 이전 edge를 현재까지 다시 확인한 사실처럼 표시하지 않고, 전체 실패 때 빈 그래프를
완료로 표시하지 않는다. 내부 보류·실패 refs·응답 전문은 공개하지 않는다.

배포 순서는 Java(V111·능력 gate) → AI(pair-group 계약) → Front(미완료 표시)다. 운영 데이터
변경·유료 재실행·기존 복구 count 초기화는 이 변경에 포함하지 않는다.

## 공개 API

작품 소유자 인증이 필요하다. 쿼리 `episodeNo`를 생략하면 가장 최근 유효 APPLIED 회차를 선택한다. 지정한 회차가 없거나 무효이면 404이며 다른 회차로 몰래 대체하지 않는다.

| 메서드 | 경로 | operationId |
|---|---|---|
| GET | `/api/v1/works/{workId}/relationship-graph?episodeNo=N` | getEpisodeRelationshipGraph |
| GET | 위 경로 + `/edges/{edgeId}?episodeNo=N` | getEpisodeRelationshipEdge |
| PATCH | 위 경로 + `/edges/{edgeId}` | updateEpisodeRelationshipEdge |
| DELETE | 위 경로 + `/edges/{edgeId}?expectedVersion=N` | deleteEpisodeRelationshipEdge |

Graph: `selectedEpisode,availableEpisodes,revision,nodes,edges,truncated,totalEdgeCount,analysisStatus,partial,updateIncomplete,incompleteEpisodeNos`.
Episode: `id,episodeNo,title`. 프론트 회차 선택은 스포일러를 줄이기 위해 N화만 사용한다.
Node: `id,name,description,profileRegistered`. v1 description은 null.
Edge: `id,sourceNodeId,targetNodeId,axis,kind,label,summary,direction,status,version,evidenceCount,lastVerifiedEpisodeNo,updateIncomplete`.
Detail: `edge,sourceNode,targetNode,evidence,version`. 근거는 `episodeId,episodeNo,episodeTitle,quote,startOffset,endOffset`이며 선택 회차 이후 근거를 보내지 않는다.

analysisStatus는 NOT_ANALYZED, PROCESSING, COMPLETED, PARTIAL, FAILED, INVALIDATED다. partial은 거절된 관측/미해결 보류/실패 또는 빠진 관계 분석이 있다는 뜻이며 사용자 검토 요청이 아니다. 최신 분석이 실패하면 기존 성공 회차 그래프를 보여주더라도 FAILED를 유지한다. 공개 그래프는 200개 edge까지 반환하고 초과 시 truncated=true와 실제 totalEdgeCount를 반환한다. 상세 API에는 이 표시 제한을 적용하지 않는다.

새 partial Context의 사용자 정정·삭제 의존성은 신규 O의 실제 stable node UUID 쌍에 한정한다. 관계 ID와 삭제 억제는 세대 간 공유되므로 같은 쌍의 다른 세대 edge도 검사한다. 무관한 쌍 정정은 진행 중인 비교를 중단시키지 않으며, 같은 쌍 정정·삭제와 원문·앞 회차 반영·세대 변경은 기존 충돌 보호를 유지한다. 이 범위는 내부 binding `dependencyPairScopeVersion=1`로 고정하고 `pairDependencyRevision`을 검증한다. 기존 `dependencyRevision`도 전체 범위 해시로 함께 보존하여 구 Java reader가 수정 없는 새 문맥을 재개할 수 있게 한다. marker가 없는 기존 PREPARED/COMPLETED Context는 종전 전체 의존성 해시를 그대로 검증하며 배포 시 새 범위로 바꾸지 않는다.

PATCH 입력은 `{expectedVersion,summary}`이며 summary는 앞뒤 공백 없는 1~500 code point다. 수정/삭제는 서사의 새 사건이 아니라 분석 오류 정정이므로 모든 회차에 적용한다. 충돌은 409다. 삭제는 canonical slot 억제 기록을 유지해 다음 분석과 재분석이 같은 연결을 되살리지 못하게 한다. v1에는 이 억제를 취소하는 공개 API가 없다. 프로필 연결/직접 관계 추가도 이번 API 범위 밖이다.

## 저장과 삭제

Flyway V101이 `relationship_graph_analyses/nodes/edges/events`와 `analysis_jobs.relationship_analysis_enabled`를 추가한다. 관계 event는 analysis+edge마다 하나이며 회차별 읽기는 최신 유효 분석만 사용한다. 관계 edge와 node는 재분석 후에도 ID를 보존해 사용자 수정과 삭제를 유지한다.

작품 파기는 관계 event→edge→node→analysis 순서로 삭제한다. 회차 원문 파기는 해당 회차 이후의 동결 Context/decision/failure/binding과 복사된 인용을 지우고 후행 결과를 무효화한다. 캐릭터 삭제는 노드의 profile FK만 null로 만들고 관계 이력을 보존한다.

## 검증

`EpisodeRelationshipGraphRulesTest`: Unicode 근거, coverage, END+ADD, 모호한 주체, 재활성화, 잘못된 참조/과거 쓰기.
`EpisodeRelationshipGraphIntegrationTest`: H2의 회차 cutoff, 멱등, 설명 수정/삭제 억제, 실패 격리, 병렬·역순 수동 작업의 노드 ID 보존과 미래 프로필 비노출, 동명·고정 인물 충돌, HOLD 재사용/소비, 원문 version 및 파기.
`EpisodeRelationshipGraphPostgresIntegrationTest`: 같은 시나리오를 V1~V101 Flyway와 JPA validate를 사용하는 격리 PostgreSQL에서 검증한다(Docker 필요).

### 2026-10-29 최종 검증 기록

- `./gradlew test bootJar --console=plain`: 성공(4분 10초). Gradle HTML/XML 기준 4,139개 중 3,880개 통과, 259개 건너뜀, 실패/오류 0. 조건부 테스트를 전부 실행했다고 해석하지 않는다. 이번 신규 관계 규칙 6개, H2 통합 12개, 실제 PostgreSQL/Flyway 통합 12개는 모두 실행·통과했다.
- 마지막 공백 정돈 뒤 `./gradlew bootJar --console=plain` 재생성 성공, `git diff --check` 통과. 산출물은 `build/libs/CatchHole-Backend-0.0.1-SNAPSHOT.jar`다.
- `EpisodeRelationshipGraphBrowserFixtureTest`는 고정된 격리 로컬 DB만 사용하는 명시적 실행용 합성 fixture이며 일반 suite에서는 건너뛴다. 이 검증에서는 별도로 실행해 성공했다. 실제 Spring/JWT/SDK/브라우저/PostgreSQL 연결로 1화의 동행 1개와 2화의 관계 3개, 회차 이후 인물·종료 상태의 숨김, 원문 근거, 설명 PATCH, 연결 DELETE, 새로고침 뒤 반영을 확인했다.
- 브라우저 인증은 서버가 발급한 임시 합성 사용자 access token을 주입했다. 로그인 UI와 refresh cookie는 이번 실제 연결 검증 범위가 아니다. DB에서 DISTRUST의 `corrected_summary`와 version 증가, FRIENDSHIP의 `suppressed=true`를 별도로 조회해 저장을 확인했다.
- 검증 종료 후 전용 Spring 서버(18233), 전용 PostgreSQL 컨테이너(18232), 임시 토큰 파일을 제거했다. 기존 로컬 DB/앱은 사용하거나 수정하지 않았다. 원고와 실제 사용자 자료는 fixture에 포함하지 않는다. 화면 기록은 프론트 저장소의 `docs/screens/gh323/episode-relationships-live-episode-1.png`, `episode-relationships-live-correction.png`에 있다.

## 최신 main 통합과 배포 순서 — 2026-10-30

main의 검수 실행·검토 종료 기능이 V92~V100을 사용하므로, 운영 미배포 관계도 migration은
`V101__add_episode_relationship_graph.sql`로 옮겼다. 기존 migration 내용은 바꾸지 않았다.
이전 V92 관계도 migration을 적용한 개인 로컬 예시 DB는 이 버전의 검증 대상으로 재사용하지 않는다.
공유·운영 DB의 Flyway 이력을 수정하거나 repair로 덮지 않고 새 격리 DB에서 전체 migration을 검증한다.

배포 순서는 Java API/DB, 지원 AI Worker, Front다. Java는 지원 광고가 없는 Worker에 관계 계약을
주지 않으며, 이미 관계 분석이 활성화된 Job은 지원 Worker만 재개할 수 있다. 기능 플래그를 끄면
신규 활성화만 멈추므로, 진행 중 관계 Job이 남은 상태의 Worker 롤백은 별도 확인이 필요하다.
기존 작품 전체의 자동 유료 백필은 하지 않는다. GH332는 [고정 선정 계정의 관계 전용 캠페인](relationship-cohort-backfill.md)만 운영 배포 후 자동 시작한다. 일반 인용의 위치가 불확실한 경우에는
[일반 인용 정책](https://github.com/catchhole-soma/catchhole-backend-java/blob/main/docs/analysis-continuity.md)의 GH323 정책으로 인용만 숨기고 설정값을 유지한다.

### V110 부분 실패 알림 (GH357)

일반 회차 SETTING_EXTRACTION에서 관계만 FAILED가 되면 기존 outbox에 RELATIONSHIP_PARTIAL을 저장하고 `관계도만 실패 · 나머지 분석 계속`으로 알린다. 관계도 전용 Job은 기존 Job FAILED 알림만 사용한다. 같은 묶음에 회차 중단 기록이 있으면 전체 중단을 우선 표시한다. 알림 추가가 실패 격리·재시도·반영 정책을 변경하지 않으며, 관계 진단의 발췌도 원문 파기 시 작품 단위로 제거한다.
