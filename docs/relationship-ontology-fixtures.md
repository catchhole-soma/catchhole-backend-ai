# 관계 의미 모델 실험: 가족 중심 규칙 회귀 자료

> 후속 상태(2026-11-19): 아래는 #438에서 자료를 준비한 당시의 범위다. 원본 24개 JSON은 변경하지 않고 #444에서 실제 Python·OWL 추론기를 실행했다. 추가 정책·손자녀 확장 및 근거/철회 검증은 [추론 비교 보고서](evaluations/relationship-inference-comparison.md)를 참고한다. 이 파일의 fixture 무결성 검사와 후속 엔진 정확성 검사를 구분한다.

- 이슈: [Java #438](https://github.com/catchhole-soma/catchhole-backend-java/issues/438)
- 상태: **제안된 실험의 2단계 산출물**. 운영 계약이나 구현된 추론 기능이 아니다.
- 입력·정답: [`tests/fixtures/relationship_ontology_cases.json`](../tests/fixtures/relationship_ontology_cases.json)
- 형식 검사: [`tests/test_relationship_ontology_fixture.py`](../tests/test_relationship_ontology_fixture.py)
- 전체 설계: [`relationship-ontology-plan.md`](relationship-ontology-plan.md)

이 파일은 “나중에 관계 추론기를 만들면 어떤 입력에서 어떤 결과가 나와야 하는가”를 고정한다. 가상 원고, 원고에서 읽었다고 가정한 관계 assertion, 사람이 작성한 정답을 함께 담았다. 현재 테스트는 파일의 구조와 근거 연결을 검사한다. 원고를 읽는 LLM, 일반 추론기, RDF/OWL, 운영 API·DB 연결은 실행하지 않는다.

이 24개 사례는 전체 온톨로지의 목적을 대표하지 않는다. [일반 관계 제품 질문과 예제](relationship-semantics-fixtures.md)가 의미 공유의 주 시험 대상이고, 이 파일은 부모 역표현·혈연 조부모와 근거 수명의 작은 회귀 묶음이다. 일반 코드로도 같은 규칙을 구현할 수 있으며, 통과 여부만으로 RDF 도입 가치를 주장하지 않는다.

## 1. 원고에서 결과까지 읽는 법

기본 이야기는 다음 세 회차다. 실제 작품이나 사용자 데이터가 아니다.

| 회차 | 가상 원고 | 사람이 정한 입력 해석 | 기대 결과 |
| --- | --- | --- | --- |
| 1화 | 도윤은 서연의 친아버지다. | `PARENT_OF(도윤, 서연, BIOLOGICAL)` | 직접 부모 사실 1개, 그 사실의 역표현인 자녀 관계 1개 |
| 2화 | 서연이 하린의 친어머니라는 소문이 돌았다. | `UNCERTAIN`, 검토 필요 | 1화 결과 유지. 도윤과 하린의 조부모 관계는 생성하지 않음 |
| 3화 | 서연은 하린의 친어머니임이 확인되었다. | 별도의 확정 `PARENT_OF(서연, 하린, BIOLOGICAL)` | 직접 부모 사실 2개, 자녀 역표현 2개, 새 혈연 조부모 관계 1개 |

3화 사례의 새 결론은 `GRANDPARENT_OF(도윤, 하린, BIOLOGICAL, generationGap=2)`다. 근거는 `a1`과 `a3`이라는 두 assertion이고, 원문 출처는 각각 `s1`과 `s3`다. `a2`의 소문은 확정으로 덮어쓰지 않고 보류된 입력으로 남는다.

출력은 다음 세 종류를 구분한다.

1. `directRelations`: 원문에서 직접 확인했다고 가정한 관계. 여러 독립 근거가 같은 관계를 지지해도 관계는 하나이고 `supportAssertionIds`에 근거들을 모은다.
2. `derivedRelations`의 `SEMANTIC_INVERSE`: 부모 관계를 뒤집어 자녀로 읽는 의미상 역표현. 기존 가족 관계 정규화에서도 다루는 의미이므로 새 추론 성과로 계산하지 않는다.
3. `derivedRelations`의 `NEW_RELATION`: 두 혈연 부모 사실을 조합한 혈연 조부모 관계. 이번 실험에서 새로 확인하려는 기능이다.

이 실험은 조부모의 역표현인 손자녀, 모든 조상, 성별에 따른 할아버지·할머니 명칭까지 자동 생성하도록 요구하지 않는다. 규칙 목록에 없는 관계를 생성하는 것은 범위 밖이다. “혈연 조부모”는 부계와 모계를 모두 포함한다.

## 2. 데이터 사전

JSON은 외부 schema 라이브러리 없이 검토할 수 있는 실험용 구조다. 제품의 DTO·DB 테이블과 일대일 대응하지 않으며 운영 API에 그대로 전달할 수 없다. ID는 각 case 안에서만 유일하면 된다. 다른 case의 같은 ID를 연결하지 않는다.

### 파일 전체

| 필드 | 의미 |
| --- | --- |
| `schemaVersion: 1` | 이 실험 파일의 구조 버전 |
| `synthetic: true` | 모든 원고·인물·근거가 합성 데이터임 |
| `status: PROPOSED_EXPERIMENT` | 아직 제품에 적용되지 않은 제안 |
| `oracle: HANDWRITTEN_NOT_ENGINE_GENERATED` | 기대 출력은 추론기로 생성하지 않고 사람이 작성함 |
| `outputSemantics: EXACT_SETS` | direct/derived 목록은 기대하는 출력 전체의 정확한 집합 |
| `rules` | 허용 규칙 ID, 전제 개수, 출력 종류, 역표현/새 관계 구분 |
| `cases` | 앞 case의 상태를 물려받지 않는 24개 독립 snapshot |

### case와 조회 범위

| 필드 | 의미 |
| --- | --- |
| `caseId`, `title`, `purpose` | 식별자, 읽기 쉬운 제목, 사례가 확인하려는 경계 |
| `snapshotRevision` | 정정·철회·원문 파기를 반영한 데이터 판 번호. 회차가 아님 |
| `snapshotThroughEpisode` | 이 합성 snapshot이 확보된 회차 |
| `query.workId` | 조회 대상 작품 |
| `query.cutoffEpisode` | 독자가 볼 수 있는 마지막 회차. 미래 출처를 사용할 수 없음 |
| `query.temporalContextId` | 관계가 성립하는 서사 시점·시간선의 식별자 |
| `query.inputMode` | `EPISODE_SNAPSHOT`: 해당 회차 범위를 표현할 수 있는 합성 입력. `CURRENT_PROFILE`: 최신 가족 사실만 있는 입력 |

회차는 **언제 독자가 알게 되는가**, `temporalContextId`는 **어느 서사 시점의 사실인가**, revision은 **데이터가 어떤 정정 상태인가**를 나타낸다. 셋을 서로 대신 사용하지 않는다. 예를 들어 3화에서 발견한 과거 사건이 1화 독자의 지식에 자동으로 추가되지는 않는다.

`CURRENT_PROFILE`의 `cutoffEpisode == snapshotThroughEpisode`는 이 fixture에서 현재 조회를 나타내기 위한 약속이다. 실제 제품이 최신 가족 PROFILE로 과거 회차를 재현할 수 있다는 뜻이 아니다. 사례 23은 최신 PROFILE밖에 없는데 과거 회차를 요청하므로 `UNSUPPORTED`다. 빈 결과를 정상 답변으로 반환하지 않는다. `EPISODE_SNAPSHOT` 입력을 운영 데이터에서 만드는 adapter 역시 아직 구현하지 않았다.

### 인물, 출처, assertion

| 필드 | 의미 |
| --- | --- |
| `entities[].entityId`, `name`, `workId` | 인물 식별자, 표시 이름, 소속 작품. 같은 이름이어도 ID가 다르면 다른 인물 |
| `identityStatus` | `RESOLVED` 또는 `UNRESOLVED`. 미해소 ID는 실험용 mention이며 제품 인물 생성 지시가 아님 |
| `sources[].sourceId`, `workId`, `episodeNo` | 출처의 ID, 작품, 발견 회차 |
| `supportStatus` | `ACTIVE` 또는 `WITHDRAWN`: 해당 지지 사실의 근거가 현재 유효한가 |
| `quoteAvailability` | `AVAILABLE` 또는 `PURGED`: 원문을 인용할 수 있는가 |
| `text`, `quote` | 합성 원문과 그 안에 실제 포함된 인용. `PURGED`이면 둘 다 `null` |
| `assertions[].assertionId` | 직접 추출했다고 가정한 진술 단위. 같은 관계를 지지하는 독립 진술은 별도 ID 가능 |
| `workId`, `subjectId`, `objectId` | 관계가 속하는 작품과 방향 있는 양 끝점 |
| `kind` | 입력은 `PARENT_OF`, `ANCESTOR_OF`, `TRUST`. 출력에는 `CHILD_OF`, `GRANDPARENT_OF`도 사용 |
| `subtype` | 가족은 `BIOLOGICAL`/`ADOPTIVE`, 신뢰는 `NONE`. 제품의 전체 subtype 목록을 복제한 것이 아님 |
| `generationGap` | 조부모·조상 관계의 명시 세대 수. 다른 종류에서는 `null` |
| `assertionState` | `ASSERTED`, `UNCERTAIN`, `NEGATED`, `HYPOTHETICAL`, `CONFLICTING` |
| `factValidity` | `VALID`, `REVIEW_REQUIRED`, `WITHDRAWN`: 실험 입력의 현재 사실 유효성 |
| `nativeTime.domain`, `scope` | `FAMILY`의 `CURRENT/HISTORICAL/UNKNOWN`, `EPISODE_RELATIONSHIP`의 `PRESENT/PAST/HYPOTHETICAL/UNKNOWN`. 서로 다른 제품 시간 enum을 자동 변환하지 않음 |
| `temporalContextId` | 같은 서사 맥락끼리만 조합하도록 사람이 부여한 실험용 ID |
| `sourceIds` | assertion을 지지하는 출처 참조 |
| `validationIssues` | self/cycle/작품/세대 모순이 이미 검출되었다고 가정한 표시. 이번 검사기가 새로 탐지하는 항목이 아님 |

사례 04·05의 `TRUST`는 제품의 `ATTITUDE + DIRECTED` 조합만을 대상으로 축약했다. `EPISODE_RELATIONSHIP`은 파이프라인 구분이지 제품 관계의 `axis`가 아니다. 전체 관계 슬롯의 axis, direction, 상태, 근거 범위까지 표현한 DTO가 아니며, 추후 adapter가 원래 값을 검증하고 보존해야 한다.

### expected와 proof

| 필드 | 의미 |
| --- | --- |
| `outcome` | `READY`: 지원되는 입력, 일부 보류 가능. `REJECTED`: 작품 혼합·self·cycle의 잘못된 입력. `UNSUPPORTED`: 필요한 과거 자료가 없음 |
| `outcomeReason` | READY이면 `null`, 나머지는 거절/미지원 이유 |
| `directRelations` | 직접 출력할 관계 전체. 각 행에 `supportAssertionIds`, `sourceIds` 포함 |
| `derivedRelations` | 파생 출력 전체. 각 행에 `derivationType`, `proofs` 포함 |
| `proofs[].ruleId` | `R_PARENT_INVERSE` 또는 `R_BIOLOGICAL_GRANDPARENT` |
| `premiseAssertionIds` | 그 proof가 사용하는 직접 assertion. 조부모 규칙은 `[A→B, B→C]`의 역할 순서로 기록. 새 결론을 다시 전제로 쓰는 재귀 추론은 범위 밖 |
| `proofs[].sourceIds` | 선언한 전제들의 출처 합집합. 없는 출처나 다른 전제의 출처로 교체하면 안 됨 |
| `withheldAssertions` | 직접 출력에서 제외할 assertion ID와 이유. 입력 assertion은 직접 근거 또는 보류 중 한 곳에 정확히 속함 |
| `forbiddenRelations` | 특히 생성하면 안 되는 관계와 이유를 적은 경계 목록 |

`directRelations`와 `derivedRelations`는 배열 순서와 무관한 **정확한 전체 집합**이다. 후속 엔진 평가에서는 누락과 추가 출력을 모두 실패로 세어야 한다. 관계 동일성에는 작품, 두 인물, kind, subtype, 시점, 세대 수가 포함된다. 동일 결론의 `proofs`는 서로 대체 가능한 독립 경로이며 사례 15에서는 두 경로를 모두 적었다.

`forbiddenRelations`는 그 입력에서 생성하면 안 되는 예시이지, 세계 전체에서 영원히 거짓이라는 선언이 아니다. 이 목록은 exhaustive하지 않으며, 전체 출력의 정확성은 direct/derived exact sets로 판단한다. 자료가 없다는 이유로 관계가 없다고 단정하지 않는다.

## 3. 24개 사례와 기대 경계

| ID | 상황 | 기대 |
| --- | --- | --- |
| 01 | 1화의 친부모 사실 | 직접 부모 + 의미상 자녀 역표현 |
| 02 | 2화에 어머니라는 소문만 존재 | 소문 보류, 혈연 조부모 없음 |
| 03 | 3화에 어머니 사실 확정 | 두 혈연 부모 전제와 두 출처를 가진 혈연 조부모 |
| 04 | 도윤→서연, 서연→하린의 신뢰 | 도윤→하린으로 전이하지 않음 |
| 05 | 도윤→서연의 신뢰만 존재 | 서연→도윤 역방향 신뢰를 생성하지 않음 |
| 06 | 친어머니라는 관계를 부정 | 긍정 부모·조부모로 사용하지 않음 |
| 07 | 어머니라면이라는 가정 | 실제 가족 사실의 전제로 사용하지 않음 |
| 08 | 2화 질의에 3화 확정 입력도 포함 | 미래 근거를 제외함 |
| 09 | 이름만 같은 서연 두 인물 | ID가 다른 두 사람을 중간 노드로 합치지 않음 |
| 10 | “그 여자”가 누구인지 미해소 | 서연으로 추정하거나 인물을 생성하지 않음 |
| 11 | 작품 A와 작품 B의 인물을 연결 | 입력 `REJECTED`, direct/derived 비움 |
| 12 | 친부모 한 단계 + 양부모 한 단계 | 양자녀 역표현 유지, 혈연 조부모 없음 |
| 13 | 서로 다른 시간선의 부모 사실 | 조회 맥락 밖 전제 보류 |
| 14 | 관계가 성립한 시점 불명 | 원문 등장 회차로 시점을 채우지 않음 |
| 15 | 같은 부모 사실의 독립 근거 두 개 | 관계는 한 개, 혈연 조부모 proof는 두 개 |
| 16 | 마지막 지지 사실 철회 | 그 부모와 혈연 조부모 출력에서 제거 |
| 17 | 한 지지 사실 철회, 다른 근거 유효 | 살아 있는 proof만 유지 |
| 18 | 원문 파기, 현재 사실과 출처 ID 유지 | 현재 관계 유지, 인용 불가를 `PURGED/null`로 표시 |
| 19 | 자기 자신을 부모로 연결 | 사전 검증 표시를 따라 `REJECTED` |
| 20 | 부모 순환 | 사전 검증 표시를 따라 `REJECTED` |
| 21 | 부모 두 단계와 명시 3세대 조상 사실 충돌 | `READY`이지만 충돌 성분을 보류, 혈연 조부모 없음 |
| 22 | 같은 관계의 긍정·부정 근거 충돌 | 충돌 전제를 보류, 유효한 다른 직접 관계는 유지 |
| 23 | 최신 가족 PROFILE로 1화 조회 요청 | `UNSUPPORTED`; 과거 결과를 재구성하지 않음 |
| 24 | 친어머니를 서연에서 민서로 정정 | 옛 assertion 철회, 새 assertion·출처 분리, 과거 혈연 조부모 제거 |

사례 16·17의 `WITHDRAWN`은 **assertion/지지 사실 철회**다. 사용자가 canonical 관계 전체를 삭제하거나 철회해서 재등장을 억제하는 상황과 다르다. 후자는 대체 근거가 남아 있어도 관계를 되살리면 안 된다. 현재 24개 사례는 그 사용자 삭제 억제 정책을 검증하지 않으며, 제품 adapter를 만들 때 별도의 입력 상태와 사례를 추가해야 한다.

원문 파기도 사실 철회와 다르다. 사례 18에서는 원문이 없어서 인용을 재구성할 수 없지만 유지된 현재 사실의 유효성이 자동으로 사라지지 않는다. 반대로 출처 ID가 남아 있다는 이유만으로 `WITHDRAWN` 사실을 다시 사용할 수 없다.

## 4. 검사 실행과 결과의 해석

프로젝트 가상환경을 활성화한 뒤 저장소 루트에서 실행한다.

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest --noconftest -q tests/test_relationship_ontology_fixture.py
```

`--noconftest`는 이 저장소의 공통 테스트 준비가 tiktoken asset 등 관계없는 환경에 접근하지 않게 한다. 자동 plugin 로딩도 끈다. Python 표준 라이브러리와 pytest만 필요하다. `.env`, API key, 모델 호출, DB, 네트워크 없이 실행한다.

테스트는 총 29개다. 24개 case 전체의 무결성 검사 1개, 문서화한 24개 ID 목록 검사 1개, 의도적으로 망가뜨린 fixture를 거절하는 검사 27개다. 손상 검사는 중복 ID, 미존재 출처, 원문에 없는 인용, 미등록 규칙, 엉뚱한 proof 출처, 보류된 전제 사용, 중복 proof, 정답/금지 출력 중복, 누락된 assertion 분류, 파기 후 남은 원문, 잘못된 kind, 미정의 필드를 다룬다. 또한 positive 출력의 미해소 인물·작품·시점 불일치, 선언된 proof의 방향·subtype·시점·세대 수·전제 역할 오류, 과거 PROFILE 질의를 READY/REJECTED로 잘못 표시한 경우를 거절한다.

검사기는 schema·enum·ID·quote 및 **정답에 선언된 전제/출처의 연결**을 검사한다. 선언된 한 부모 전제의 역방향과 subtype 보존, 선언된 두 전제의 `A→B→C`와 결과 `A→C`의 대응을 확인한다. positive 출력은 해소된 같은 작품 인물과 조회 시점을 요구하고, 현재 PROFILE의 과거 질의는 UNSUPPORTED여야 한다. 이 검사는 이미 작성된 proof 한 개씩을 대조할 뿐, 그래프에서 전제를 찾거나 전체 정답·누락 관계를 생성하지 않으며 정답 JSON을 고치지도 않는다. cycle·세대 모순·자연어 지칭의 의미를 새로 판정하지 않는다. 따라서 `29 passed`는 **수작업 정답 파일의 무결성 통과**이고, 추론 정확도 100%나 운영 모델 품질 측정값이 아니다.

후속 단계에서는 이 고정 파일을 읽는 실제 엔진에 각 snapshot을 입력하고, 나온 전체 결과와 수작업 exact sets를 비교해야 한다. 제품 데이터 adapter, 사용자 삭제 억제, 미지원 subtype·시간 맥락의 처리도 그때 별도로 검증한다. 이번 단계에서는 그 엔진이나 운영 적용을 구현하지 않았다.
