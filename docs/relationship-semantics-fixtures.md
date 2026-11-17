# 일반 관계의 의미를 보존하는 제품 질문 예제

- 이슈: [Java #438](https://github.com/catchhole-soma/catchhole-backend-java/issues/438)
- 상태: **1·2단계 보완용 제안과 수작업 예제**. 실제 제품 연동이 아니다.
- 데이터: [`tests/fixtures/relationship_semantics_cases.json`](../tests/fixtures/relationship_semantics_cases.json)
- 무결성 검사: [`tests/test_relationship_semantics_fixture.py`](../tests/test_relationship_semantics_fixture.py)
- 전체 계획: [`relationship-ontology-plan.md`](relationship-ontology-plan.md)
- 기존 가족 24개 사례: [`relationship-ontology-fixtures.md`](relationship-ontology-fixtures.md)
- 현재 제품 의미의 기준: [`episode-relationships.md`](episode-relationships.md)

가족 사례는 부모 두 단계를 연결하는 작은 규칙을 설명하기 좋다. 그러나 관계도에서 더 자주 필요한 질문은 “나를 신뢰하는 사람이 나도 신뢰하는가”, “같이 행동했으면 친구인가”, “동료인데 불신하면 모순인가”다. 이번 예제는 새 관계를 더 많이 만드는 것보다 **원래 관계의 방향·종류·확정 여부·시점을 여러 기능이 같은 의미로 읽는가**를 확인하려고 추가했다.

합성 원고 10사례에 11개 질의를 두고, 각 질의에 관계도·검색·검수 예시 3개씩 총 33개를 수작업으로 적었다. 기존 가족 fixture와 별도 파일이며 기존 규칙이나 정답을 바꾸지 않는다. LLM 추출, 관계 추론, adapter, RDF/OWL, 검색 API, 실제 검수 판정은 실행하지 않는다.

## 1. 같은 사실을 세 곳에서 사용하는 이유

예를 들어 원고에 “도윤은 서연을 신뢰한다”, “서연은 도윤을 불신한다”가 있다. 두 문장을 다음처럼 독립된 사실로 보존한다.

| 사실 참조 | 확정된 slot |
| --- | --- |
| `f1` | 도윤 → 서연, `ATTITUDE / TRUST / DIRECTED` |
| `f2` | 서연 → 도윤, `ATTITUDE / DISTRUST / DIRECTED` |

| 미래 consumer 예시 | 같은 `f1`, `f2`로 확인할 내용 |
| --- | --- |
| 관계도 | 두 방향을 각각 표시하고 상호 신뢰 선으로 합치지 않는다. |
| 검색 | “서로 신뢰하는가?”라는 질문에 두 사람의 서로 다른 태도를 구분한다. |
| 검수 | “서로 신뢰한다”는 새 문장을 검토할 때 같은 두 사실과 출처를 대조 대상으로 삼는다. |

이 세 예시가 필요한 이유는 규칙 하나가 작동해도 다른 기능이 방향을 무시하거나 추측을 확정으로 읽으면 사용자에게 서로 다른 답을 보여줄 수 있기 때문이다. 한 번 정한 사실 식별자와 의미를 재사용할 가치가 있는지, 어떤 경계가 기능 사이에서 유지되어야 하는지를 먼저 검토한다.

각 consumer의 `factRefs`는 의도적으로 같은 질의의 `expected.factRefs`와 일치하도록 작성했다. **이 좁은 예제에서 같은 문제를 세 방식으로 읽는 약속**이며, 모든 실제 검색·검수가 관계도 전체 사실을 항상 받아야 한다는 전역 규칙은 아니다. 소비 기능의 `expectedText`는 사람이 쓴 설명 예시이고, 실제 검색 답변·검수 결과나 자연어 의미 채점 결과가 아니다.

## 2. 사례와 제품 질문

| ID | 제품 질문 | 상황 | 기대하는 의미 보존 |
| --- | --- | --- | --- |
| 01 | P1 방향과 축의 공존 | 도윤의 신뢰와 서연의 불신 | 방향별 태도를 보존하고 상호 신뢰를 만들지 않음 |
| 02 | P2 행동과 지속 관계 | 도윤의 도움 행동 + 공식 협력자 관계 | 같은 `COOPERATION`이라도 다른 axis/direction의 두 slot을 유지. 친구로 확대하지 않음 |
| 03 | P1 방향과 축의 공존 | 동료이면서 상대를 불신 | 사회적 관계와 태도가 함께 존재함. 그 이유만으로 모순이라고 하지 않음 |
| 04 | P3 독자 지식과 서사 시간 | 첫 원정부터 이어진 동료 관계를 3화에서 확인 | 1화 질의에서는 제외, 3화에서는 같은 `f3`를 사용 |
| 05 | P3 독자 지식과 서사 시간 | 첫 원정 당일의 전체 관계 복원 요청 | 임의 서사 시점 복원은 `UNSUPPORTED` |
| 06 | P4 확정·근거·미해소 | 신뢰한다는 추측과 가정 | 주장은 보존하지만 확정 fact를 만들지 않음 |
| 07 | P5 종료·정정·삭제 | 동료 관계를 명시적으로 끝냄 | `ENDED`로 남기고 종료 근거를 별도 보존 |
| 08 | P5 종료·정정·삭제 | 동료였다는 기존 기록이 오류여서 정정 | 사실을 철회하되 서사 속 종료 사건으로 만들지 않음 |
| 09 | P5 종료·정정·삭제 | 사용자가 slot을 삭제한 뒤에도 독립 근거 두 개가 존재 | canonical slot 억제로 재등장을 막음 |
| 10 | P4 확정·근거·미해소 | 이름이 같은 서연 두 후보 | 이름만으로 병합하거나 특정 인물의 사실로 확정하지 않음 |

사례 02에는 다음 두 slot이 동시에 있다.

```text
f1: 도윤 → 서연, INTERACTION / COOPERATION / DIRECTED
f2: 도윤 ↔ 서연, SOCIAL / COOPERATION / MUTUAL
```

`kind=COOPERATION`이라는 이유로 axis나 direction을 하나로 정하지 않는다. 제품 slot의 동일성은 **양 끝점 + axis + kind + direction**이다. `DIRECTED`는 끝점 순서를 보존하고, `MUTUAL/UNDIRECTED`는 대칭으로 비교한다. 같은 kind의 다른 slot은 중복이 아니다. 특정 사례의 `TRUST`가 `ATTITUDE/DIRECTED`라는 사실도 모든 TRUST의 전역 제약으로 일반화하지 않는다.

## 3. 독립 데이터 계약

이 JSON은 실험용 분리 모델이다. 실제 `RelationshipGraphContract`의 DTO를 복제하거나 새 API 계약을 선언한 것이 아니다. 이름 해소·PRESENT 판정·확정·정정은 이미 사람이 판정했다고 가정한다. 각 case는 독립 snapshot이고 ID의 유효 범위는 그 case 안이다. case 간 ID를 연결하지 않는다.

여기서 claim은 **사람이 의미 방향까지 정규화한 진술**이다. DIRECTED fact에 연결할 때 claim의 subject 후보는 관계의 의미상 출발점, object 후보는 도착점을 뜻한다. 실제 제품 observation의 `source/target` 순서가 이 의미 방향을 보장하는 것은 아니므로 그 필드를 그대로 복사하는 adapter를 만들면 안 된다. 방향 해석까지 끝난 결과를 어떻게 연결할지는 후속 adapter 설계 대상이다. 철회된 claim도 어느 관계에 대한 기록이었는지는 보존하므로 endpoint 참조 검사를 받는다.

### 최상위와 case

| 필드 | 의미 |
| --- | --- |
| `schemaVersion: 1` | 이 예제 파일의 구조 버전 |
| `synthetic: true` | 원고·인물·사실 모두 합성 |
| `status: PROPOSED_CONSUMER_EXAMPLES` | 미래 기능에서 읽는 방식을 제안한 예제 |
| `oracle: HANDWRITTEN_NOT_ENGINE_GENERATED` | 정답을 엔진에서 생성하지 않고 사람이 작성함 |
| `outputSemantics: EXACT_FACT_REF_SETS` | 질의별 사용할 fact 참조 전체를 정확한 집합으로 지정 |
| `productIntegration: false` | 운영 연동·공통 저장소·공통 조회가 구현되지 않음 |
| `caseId`, `title`, `purpose`, `productQuestionId` | 사례 식별, 설명, P1~P5 연결 |
| `workId` | 작품 경계 |
| `snapshotThroughEpisodeNo` | 이 입력 snapshot이 확보된 회차 |
| `snapshotRevision` | 정정·삭제 상태를 반영한 데이터 판 번호. 사건 시점이나 독자 회차가 아님 |
| `nodes`, `sources`, `claims`, `facts`, `suppressions`, `queries` | 인물·원문·주장·확정 관계·사용자 억제·질의를 별도 보존 |

### 원문 주장과 확정 관계

| 필드 | 의미 |
| --- | --- |
| `nodes[].nodeRef`, `name`, `workId` | 인물 ID, 표시 이름, 작품. 동명이인도 서로 다른 nodeRef |
| `sources[].sourceRef`, `episodeNo`, `text`, `quote` | 출처 ID, 원문 회차, 합성 원문, 그 안에 포함된 정확 인용. source도 `workId` 보존 |
| `claims[].claimRef`, `sourceRef` | 원문을 해석한 주장과 출처 연결 |
| `subjectCandidateRefs`, `objectCandidateRefs` | 주장 양쪽의 후보 인물 목록. 둘 이상이면 미해소일 수 있음 |
| `statementMode` | `ASSERTED`, `UNCERTAIN`, `HYPOTHETICAL`: 단정·추측·가정의 차이 |
| `curationStatus` | `CONFIRMED`, `HOLD`, `RETRACTED`: 사람이 지정한 처리 상태. 원문 표현과 별개 |
| `temporalScope` | 제품 어휘인 `PRESENT`, `PAST`, `HYPOTHETICAL`, `UNKNOWN`을 보존. 확정 slot의 근거는 PRESENT만 사용 |
| `narrativeContextId` | `story-present`, `since-first-expedition` 같은 원문의 서사 문맥 표지. 시간 계산이나 interval 모델은 아님 |
| `facts[].factRef` | 확정된 관계 snapshot의 참조. source/claim ID와 구분 |
| `facts[].slot` | `workId`, `sourceNodeRef`, `targetNodeRef`, `axis`, `kind`, `direction`의 명시 tuple |
| `status` | `ACTIVE` 또는 `ENDED`: 서사 속 관계의 수명 |
| `factValidity` | `VALID` 또는 `RETRACTED`: 기록으로서 유지할 사실인가. ENDED와 다른 차원 |
| `knownFromEpisodeNo` | 독자 지식으로 처음 확인된 회차 |
| `lastChangedEpisodeNo` | 이 합성 snapshot에 채택한 상태·근거가 마지막으로 반영되었다고 지정한 회차. 시스템 수정 시각이나 작중 사건 시점, 전체 event 이력이 아님 |
| `supportClaimRefs` | 관계를 직접 지지하는 주장. 독립 근거가 둘이면 둘 다 포함 |
| `endClaimRefs` | 명시적인 종료 주장. ENDED에만 존재하며 최초 지지 주장과 겹치지 않음 |
| `correctionSourceRefs` | 잘못된 기록의 정정 출처. 서사 속 END 근거가 아님 |

`axis`는 SOCIAL/ATTITUDE/INTERACTION, `kind`는 현재 제품의 11개 종류, `direction`은 DIRECTED/MUTUAL/UNDIRECTED를 그대로 별도 필드로 둔다. 검사기에 `kind → axis/direction` 표를 만들지 않는다. 이 예제의 주장과 사실 사이 연결은 사람이 지정했으며 자연어에서 tuple을 자동 추출하는 코드는 없다.

### 사용자 삭제 억제

| 필드 | 의미 |
| --- | --- |
| `suppressions[].suppressionRef` | 사용자 삭제 억제 기록의 ID |
| `slot` | 억제할 canonical slot. 특정 근거 하나의 ID가 아님 |
| `reason: USER_DELETE` | 서사의 새 사건이 아닌 사용자의 삭제 |
| `appliesTo: ALL_EPISODES` | 현재 제품처럼 모든 회차에 적용할 정정·삭제 의미 |
| `recordedRevision` | 삭제가 반영된 데이터 판 번호 |

사례 09에서는 사실 `f1`과 독립 근거 `c1`, `c2`가 남아 있어도 slot 억제 때문에 사용할 fact 목록에서 제외한다. 근거를 한 개 더 찾았다는 이유로 자동 관계 생성이 삭제된 연결을 되살려서는 안 된다.

검색·검수 예시에서도 같은 확정 관계 참조를 제외하는 것은 **이번 설계의 consumer 정책 제안**이다. 현재 검색·검수에 이 정책이 구현되어 있다는 뜻이 아니다. 원문 자체가 삭제되었거나 모든 원문 검색이 금지된다는 뜻도 아니다. 이번 예제는 공유할 확정 관계 사실을 어디까지 채택할지에 한정한다. 원문 검색의 권한과 인용 정책은 별도다.

### 질의와 수작업 기대값

| 필드 | 의미 |
| --- | --- |
| `queryRef` | case 안의 질의 ID |
| `mode` | `READER_KNOWLEDGE`: 주어진 독자 회차 범위. `STORY_TIME`: 임의 작중 시간선 복원 요청, 현재 미지원 |
| `cutoffEpisodeNo` | 사용할 수 있는 독자 지식·근거의 마지막 회차 |
| `storyContextId` | STORY_TIME에서 원하는 작중 문맥, READER_KNOWLEDGE에서는 `null` |
| `expected.status`, `reason` | `READY/null` 또는 `UNSUPPORTED/STORY_TIME_RECONSTRUCTION_NOT_SUPPORTED` |
| `factRefs` | 이 질의에서 채택할 fact ID 전체. 순서와 무관한 정확 집합 |
| `excludedFacts` | 나머지 fact와 제외 이유: FUTURE_KNOWLEDGE/RETRACTED_FACT/USER_SUPPRESSED/UNSUPPORTED_QUERY |
| `heldClaimRefs` | 아직 확정 fact로 사용할 수 없는 보류 주장 참조 |
| `forbiddenSlots` | 특히 만들어서는 안 되는 slot과 이유. 보편적으로 거짓이라는 선언은 아님 |
| `consumerExamples` | RELATIONSHIP_GRAPH/SEARCH/REVIEW 각각의 질문, 수작업 expectedText, 동일 factRefs |

각 fact는 `factRefs` 또는 `excludedFacts`에 한 번만 속한다. 확정 사실이 없다는 것은 그 관계가 세상에 없다는 의미가 아니다. 불확실한 주장이나 미해소 이름이 있을 때는 “이 자료로 확정할 수 없다”로 남긴다. `forbiddenSlots`는 집중적으로 확인할 경계이고, 소비 가능한 전체 집합은 `factRefs`로 지정한다.

## 4. 시간과 수명에서 약속하지 않는 것

사례 04의 원문은 “첫 원정 때부터 **지금까지** 동료였다”다. 현재도 이어지는 관계가 3화에서 새로 드러난 것이므로 claim의 제품 `temporalScope`는 PRESENT다. 과거 시작 문맥이 있다는 이유로 제품 PAST 관측을 확정 slot으로 바꾸지 않는다. 1화 질의에서 미래 사실을 제외하는 것은 `knownFromEpisodeNo=3`을 따르는 것이며, 임의 시간선 복원 기능이 아니다.

사례 07의 최종 snapshot은 ENDED와 종료 근거를 갖지만, 그 행 하나로 1화 당시의 ACTIVE 상태를 재구성하지 않는다. 이 사례에는 3화 질의만 있다. 후속 단계에서 과거 상태 질의를 지원하려면 그 회차에 맞게 선택한 상태나 event 이력이 별도로 필요하다. `lastChangedEpisodeNo`가 질의 회차보다 뒤인 snapshot을 과거 상태인 것처럼 사용하는 것은 검사에서 거절한다. 이 fixture의 READY 질의 범위 밖인 임의 과거 상태는 지원한다고 약속하지 않는다.

사례 05는 첫 원정 당일의 **전체 인물 관계 상태를 빠짐없이 복원**하는 요청이라 미지원이다. 원문에 명시된 개별 관계 진술에 제한적으로 답하는 검색까지 불가능하다는 뜻은 아니다.

사례 08의 `factValidity=RETRACTED`와 `correctionSourceRefs`는 실제 일반 관계 API의 필드나 구현된 자동 정정 흐름이 아니다. 사람이 정정 상태를 주입해 “잘못된 기록의 철회”와 “서사의 관계 종료”를 구분해 보는 실험이다. 현재 일반 관계 API의 PATCH는 summary 수정이고 DELETE는 canonical slot 억제다. 과거 오류를 END로 바꾸는 API라고 해석하지 않는다.

이 사례의 정정 출처는 3화에 들어 있는 합성 정정문이고, `lastChangedEpisodeNo=3`은 그 근거를 이 snapshot에 반영했다고 지정한 회차다. 지원·종료·정정 출처의 회차는 이 값보다 뒤일 수 없도록 검사한다. 실제 사용자가 시스템에서 정정한 시점은 `snapshotRevision`과 별개의 수정 시각/이력으로 다뤄야 하며, 이 fixture는 시스템 수정 시각을 서사 회차로 환산하지 않는다.

종료·정정·삭제의 구분은 다음과 같다.

| 상황 | 원래 관계 기록 | 현재 소비 예시 |
| --- | --- | --- |
| 서사에서 명시적으로 종료 | `VALID + ENDED`, 종료 claim 존재 | 종료된 관계라는 사실을 보존 |
| 잘못된 기록을 정정 | `RETRACTED`, 정정 source 존재 | 확정 사실에서 제외. 종료 사건으로 해석하지 않음 |
| 사용자 slot 삭제 | 사실 근거와 별도의 suppression | 독립 근거가 남아도 그 slot을 자동 복원하지 않음 |

## 5. 실행과 확인 범위

프로젝트 가상환경에서 저장소 루트 기준으로 실행한다.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest --noconftest -q tests/test_relationship_semantics_fixture.py
```

표준 라이브러리와 pytest만 필요하다. `--noconftest`와 plugin 자동 로딩 해제로 기존 테스트 준비 과정의 tiktoken asset 등 관계없는 의존성을 피한다. 모델·DB·네트워크·환경변수 설정을 사용하지 않는다.

현재 검사 결과는 **21 passed**다. 전체 무결성 1개, 10사례/11질의/P1~P5 및 두 COOPERATION slot 보존 확인 1개, 손상된 데이터를 거절하는 검사 19개다. 손상 검사는 잘못된 인용·참조, consumer별 사실 불일치, 추측·미해소 주장의 확정 승격, 미래 사실·근거 유입, 서로 다른 slot의 합침, 종료 근거 누락, 정정을 END로 변경, 삭제 관계 부활, 삭제와 철회의 혼동, 미지원 질의의 성공 표시, 방향 변조, 잘못된 axis를 다룬다. 철회된 사실의 인물 참조 변조와 종료·정정 근거보다 앞선 상태 메타데이터도 거절한다.

검사기는 사람이 선언한 참조와 상태의 무결성을 검사한다. 자연어 원고의 의미나 검색 답변·검수 설명의 정답성을 자동 판정하지 않는다. 사건 이력 reducer, 실제 조회 필터, 추론 규칙, 정답 자동 생성도 구현하지 않는다. `21 passed`는 이 예제의 일관성을 확인했다는 뜻이며 실제 제품 성능·모델 정확도·사용자 가치가 측정됐다는 뜻이 아니다.

후속 RDF 표현이나 다른 저장 방식을 실험하더라도 원문 주장, 확정 slot, 관계 수명, 사실 철회, 지식 회차, 사용자 삭제 억제를 하나의 무조건 참인 관계로 합치지 않는 것이 이 파일의 준비 작업이다. 아직 어떤 RDF 문법이나 라이브러리로 직렬화할지는 구현하지 않았다. 다음 단계의 판단 근거는 “새 표현으로 같은 의미와 참조가 유지되고 세 소비 기능의 오해를 줄이는가”이지, 특정 기술을 도입했다는 사실 자체가 아니다.
