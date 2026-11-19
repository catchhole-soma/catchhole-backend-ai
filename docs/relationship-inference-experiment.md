# Python·OWL 관계 추론 비교 실험

- 추적: [Java #444](https://github.com/catchhole-soma/catchhole-backend-java/issues/444)
- 범위: 합성 입력의 오프라인 추론. `app/`, DB, LLM, 운영 API와 배포는 변경하지 않는다.
- 선행: [가족 의미·정답 자료](relationship-ontology-fixtures.md), [전체 계획](relationship-ontology-plan.md).
- 표현·검증·조회 실험 [AI #157](https://github.com/catchhole-soma/catchhole-backend-ai/pull/157)과 독립된 다음 단계다. 그 PR의 속도와 이번 추론 시간을 같은 측정으로 취급하지 않는다.

## 무엇을 비교하는가

질문은 “조회 회차·서사 맥락에서 혈연 조부모가 누구이고, 어떤 전제로 알았으며, 그 전제가 사라지면 무엇이 바뀌는가?”다. 직접 부모 사실 두 개를 연결한 조부모는 입력에 없던 결론이다. 부모를 자녀로 뒤집은 표현은 같은 사실이므로 새 지식으로 세지 않는다.

| 역할 | 구현 | 경계 |
| --- | --- | --- |
| 공통 입력·정책 | 엄격한 Python 자료 검사, 작품/맥락/회차/인물/출처/삭제 적격성 | OWL이 자동으로 해결한 기능으로 계산하지 않음 |
| B | 타입이 있는 규칙 선언 + subject/object 인덱스 + 순서 있는 전제 조인 | Datalog 방식의 원리를 사용하는 제한적 실행기이며 범용 Datalog 엔진은 아님 |
| C | RDFLib 그래프 + 실제 owlrl OWL 2 RL closure | Python으로 호출하지만 결론은 OWL 엔진에서 계산 |
| C의 설명 | 원래 assertion을 별도로 색인해 모든 규칙 증명 경로를 복원 | owlrl의 내장 설명 기능이 아니며 B의 결과를 사용하지 않음 |
| 출력·평가 | 공통 표현 정리/삭제 필터, 별도 정답 평가기 | 모든 관계·proof·출처·제외 사유를 정확히 비교 |

RDF는 데이터를 그래프로 표현하는 방식, OWL은 관계 의미·공리를 표현하는 언어다. SHACL은 데이터 모양을 검증하는 별도 기술이다. 이번 단계는 OWL 추론을 실행하며, 이전 SHACL 검사 시간을 추론 비용으로 인용하지 않는다. 설치 extra의 pySHACL은 선행 실험과 같은 묶음을 유지하기 위한 것으로 이 실행기가 호출하지 않는다.

## 입력과 정책

기존 24개 가족 fixture는 변경하지 않는다. 추가 정책 입력은 `tests/fixtures/relationship_inference_policy_cases.json`의 schemaVersion 2다. `parse_cases`는 실행 필드만 읽으며 `expected`, 설명용 금지 목록, 문서에 적힌 규칙의 값은 계산에 사용하지 않는다. 실행 규칙은 버전 관리되는 Python 선언과 Turtle 파일에 고정되어 있다. 모르는 필드, 중복 ID, 없는 참조, 잘못된 enum/자료형과 파기 후 남아 있는 원문은 실패한다.

- 원래 assertion/source 목록은 입력 기록에 남는다. 적격 사본의 `sourceIds`만 현재 유효하고 독자 회차 이내인 출처로 제한한다.
- 같은 assertion의 여러 출처는 대체 지지(OR)다. 출처 두 개가 proof 두 개를 뜻하지 않는다. 서로 다른 assertion 조합은 독립 proof다.
- 원문 `PURGED`와 사실/출처 `WITHDRAWN`은 다르다. 원문만 파기되면 유효한 관계와 출처 ID는 유지하되 인용을 복원하지 않는다.
- 작품·해소된 인물 ID·서사 맥락이 맞아야 한다. 같은 이름을 합치지 않는다. 불확실·부정·가정·검토·미래 입력은 전제가 되지 않는다.
- 현재 PROFILE만으로 과거 회차를 재구성하려는 요청은 `UNSUPPORTED`다. 빈 정상 결과로 위장하지 않는다.
- `validationIssues`는 선행 검사에서 제공된 표시다. cycle/세대 모순을 이번 엔진이 새로 발견했다고 말하지 않는다.

사용자 삭제는 `suppressions`에 기록한다. 관계 동일성은 작품·양 끝점·kind·subtype·맥락·세대 수 전체다. 부모/자녀와 조부모/손자녀는 같은 의미의 삭제 대상으로 정규화한다. `recordedRevision <= snapshotRevision`이고 `active=true`인 현재 삭제를 같은 맥락의 모든 독자 회차 조회에 적용한다. 이는 과거 revision 자체의 복원이 아니다.

부모 전체 삭제는 모든 해당 전제와 역표현, 그 전제를 쓰는 추론 경로를 막는다. 조부모 결론만 삭제하면 부모 전제는 유지하고 해당 조부모 결론을 제외한다. 다른 유효한 경로가 생겨도 활성 삭제를 자동 해제하지 않는다. 파생 결론 제외는 `excludedDerivedRelations`에 별도로 설명한다.

## 기본 규칙과 근거

| 규칙 | 전제 | 결론 |
| --- | --- | --- |
| R_PARENT_INVERSE | 직접 PARENT_OF(A,B,subtype) | CHILD_OF(B,A,같은 subtype), SEMANTIC_INVERSE |
| R_BIOLOGICAL_GRANDPARENT | 직접 혈연 부모 A→B, 직접 혈연 부모 B→C | 혈연 GRANDPARENT_OF(A,C,gap=2), NEW_RELATION |

기본 규칙은 직접 assertion만 소비한다. 입양과 혈연을 섞거나 TRUST를 전이·대칭 관계로 해석하지 않으며, 미지 인물·모든 조상·성별 명칭을 만들지 않는다.

`proof = ruleId + 순서 있는 premiseAssertionIds + 적격 sourceIds의 합집합`이다. A→B의 독립 assertion이 두 개이고 B→C도 두 개면 조부모 결론 하나에 네 proof가 있다. RDF 그래프의 동일 edge가 하나로 합쳐져도 원래 assertion 색인에서 네 경로를 모두 복원해야 한다. C는 엔진 결론 집합과 복원 가능한 증명 집합이 다르면 실패한다. 예상하지 않은 제품 predicate/인물/동일성 추론도 숨기지 않고 실패시킨다.

각 조회/revision마다 적격 직접 사실에서 새 인덱스·그래프를 만든다. 이전 closure에 새 사실을 덧붙이는 방식이 아니다. 한 경로 철회 시 다른 경로만 유지하고, 마지막 경로가 사라지면 결론도 사라진다. 이 검증은 전체 재계산의 정확성이다. 증분 truth maintenance, DB 트랜잭션이나 운영 동시성을 구현한 것은 아니다.

## 실행과 기록

```bash
python -m pip install -e '.[dev,ontology]'
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest --noconftest -q tests/test_relationship_inference*.py tests/test_relationship_ontology_fixture.py
python -m scripts.relationship_inference_experiment.run --input tests/fixtures/relationship_ontology_cases.json --oracle tests/fixtures/relationship_ontology_cases.json --output /tmp/inference-family
python -m scripts.relationship_inference_experiment.run --input tests/fixtures/relationship_inference_policy_cases.json --oracle tests/fixtures/relationship_inference_policy_cases.json --output /tmp/inference-policy
python -m scripts.relationship_inference_experiment.benchmark --input /tmp/inference-family/inputs.json --input /tmp/inference-policy/inputs.json --output /tmp/inference-benchmark
```

출력 디렉터리는 새 경로여야 한다. `inputs.json`, `actual-B.json`, `actual-C.json`을 먼저 저장한 뒤 별도 `--oracle` 파일을 열어 채점한다. 정답 파일의 실행 입력이 다르면 채점하지 않는다. `evaluation.json`, `manifest.json`, `measurements.json`에는 전체 정답 일치 여부, 코드 commit/dirty/hash·규칙 hash·입력/정답/결과 hash와 실제 실행 시간을 기록한다. 정답을 제거하거나 바꾸어도 계산은 변하지 않아야 한다.

벤치마크는 사례 묶음과 **한 작품·한 맥락 안의** 100/1,000 직접 사실을 사용한다. 규모 자료는 서로 분리된 2단계 경로로 결과량을 제한하며, 사실 수만 늘리고 출력을 숨기지 않는다. 각 방식 4개 별도 프로세스를 B,C/C,B 순서로 번갈아 실행한다. 초기 계산과 첫 출처 철회 후 전체 재계산 모두 결과 hash를 비교한다. 규모 자료에는 독립적으로 계산한 예상 관계/proof 개수도 적용한다.

parse·공통 적격성(재검증 포함)·모델 구성·추론·proof·출력 단계와 전체 시간을 나눈다. `totalMs`는 엔진 import와 JSON 파일 I/O를 제외한다. 별도로 프로세스 전체 경과와 엔진 import 시간을 기록한다. 메모리 추적은 별도 실행에서 하므로 추적 오버헤드가 시간 중앙값에 섞이지 않는다. tracemalloc peak는 이 작업의 Python 할당, RSS는 라이브러리 import를 포함한 프로세스 최대 상주 메모리로 서로 다른 지표다. 입력 그래프의 크기·규칙 수·출력 proof 수에 따라 비용이 달라지므로 운영 성능으로 일반화할 수 없다.

## 읽는 순서

1. `input.py` / `policy.py`: 무엇이 전제에 들어갈 수 있는지.
2. `python_rules.py` / `python_engine.py`: 인덱스로 전제를 연결하고 증명을 만드는 법.
3. `owl/base.ttl` / `owl_engine.py`: 같은 의미를 OWL로 선언하고 closure·별도 증명 복원을 대조하는 법.
4. `output.py` / `evaluate.py`: 삭제와 전체 결과 계약을 검증하는 법.
5. `run.py` / `benchmark.py`: 정답 분리와 비용 기록을 재현하는 법.

도입 판단은 “규칙 추론 기능이 유용한가”와 “OWL 표준 도구가 Python보다 어떤 추가 이점을 주는가”를 나눈다. 합성 자료 통과만으로 실제 소비자 품질·유지보수 절감·운영 도입을 확정하지 않는다.
