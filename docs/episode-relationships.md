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

실패 요청은 `{formatVersion:1,sourceHash,contextToken,failureCode}`다. failureCode는 INVALID_RESPONSE, EXTRACTION_FAILED, COMPARISON_FAILED, INPUT_TOO_LARGE다.

한 prepare 최대 신규 관측 384개, 관측당 인용 1~12개, 한 인용 480 code point 이하, 관측당 인용 총 4000 code point 이하, 이름/mention 100 이하, 관측 summary 300 이하. 관계 설명 summary는 500 이하. 완료 결정 최대 512개, label 60 이하, reason 500 이하. 관련 H가 384개 또는 비교 문맥의 노드/관계가 각각 1536개를 초과하면 자르지 않고 FAILED/INPUT_TOO_LARGE Context를 저장해 반환한다. `rejectedObservationCount`는 검증 탈락을 성공한 빈 결과로 숨기지 않기 위한 비음수 개수다.

## 공개 API

작품 소유자 인증이 필요하다. 쿼리 `episodeNo`를 생략하면 가장 최근 유효 APPLIED 회차를 선택한다. 지정한 회차가 없거나 무효이면 404이며 다른 회차로 몰래 대체하지 않는다.

| 메서드 | 경로 | operationId |
|---|---|---|
| GET | `/api/v1/works/{workId}/relationship-graph?episodeNo=N` | getEpisodeRelationshipGraph |
| GET | 위 경로 + `/edges/{edgeId}?episodeNo=N` | getEpisodeRelationshipEdge |
| PATCH | 위 경로 + `/edges/{edgeId}` | updateEpisodeRelationshipEdge |
| DELETE | 위 경로 + `/edges/{edgeId}?expectedVersion=N` | deleteEpisodeRelationshipEdge |

Graph: `selectedEpisode,availableEpisodes,revision,nodes,edges,truncated,totalEdgeCount,analysisStatus,partial`.
Episode: `id,episodeNo,title`. 프론트 회차 선택은 스포일러를 줄이기 위해 N화만 사용한다.
Node: `id,name,description,profileRegistered`. v1 description은 null.
Edge: `id,sourceNodeId,targetNodeId,axis,kind,label,summary,direction,status,version,evidenceCount`.
Detail: `edge,sourceNode,targetNode,evidence,version`. 근거는 `episodeId,episodeNo,episodeTitle,quote,startOffset,endOffset`이며 선택 회차 이후 근거를 보내지 않는다.

analysisStatus는 NOT_ANALYZED, PROCESSING, COMPLETED, PARTIAL, FAILED, INVALIDATED다. partial은 거절된 관측/미해결 보류/실패 또는 빠진 관계 분석이 있다는 뜻이며 사용자 검토 요청이 아니다. 최신 분석이 실패하면 기존 성공 회차 그래프를 보여주더라도 FAILED를 유지한다. 공개 그래프는 200개 edge까지 반환하고 초과 시 truncated=true와 실제 totalEdgeCount를 반환한다. 상세 API에는 이 표시 제한을 적용하지 않는다.

PATCH 입력은 `{expectedVersion,summary}`이며 summary는 앞뒤 공백 없는 1~500 code point다. 수정/삭제는 서사의 새 사건이 아니라 분석 오류 정정이므로 모든 회차에 적용한다. 충돌은 409다. 삭제는 canonical slot 억제 기록을 유지해 다음 분석과 재분석이 같은 연결을 되살리지 못하게 한다. v1에는 이 억제를 취소하는 공개 API가 없다. 프로필 연결/직접 관계 추가도 이번 API 범위 밖이다.

## 저장과 삭제

Flyway V101이 `relationship_graph_analyses/nodes/edges/events`와 `analysis_jobs.relationship_analysis_enabled`를 추가한다. 관계 event는 analysis+edge마다 하나이며 회차별 읽기는 최신 유효 분석만 사용한다. 관계 edge와 node는 재분석 후에도 ID를 보존해 사용자 수정과 삭제를 유지한다.

작품 파기는 관계 event→edge→node→analysis 순서로 삭제한다. 회차 원문 파기는 해당 회차 이후의 동결 Context/decision/binding과 복사된 인용을 지우고 후행 결과를 무효화한다. 캐릭터 삭제는 노드의 profile FK만 null로 만들고 관계 이력을 보존한다.

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
