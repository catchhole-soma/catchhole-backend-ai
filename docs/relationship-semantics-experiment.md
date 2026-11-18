# 관계 의미 조회·검증 비교 실험 — 3단계

- 추적: [Java #442](https://github.com/catchhole-soma/catchhole-backend-java/issues/442)
- 선행 준비: [Java #438](https://github.com/catchhole-soma/catchhole-backend-java/issues/438), [AI #156](https://github.com/catchhole-soma/catchhole-backend-ai/pull/156)
- 입력: `tests/fixtures/relationship_semantics_cases.json` (가상 인물/원문 10사례, 11질의)
- 결과·측정·채택 판단은 [비교 보고서](evaluations/relationship-semantics-comparison.md)를 따른다.

## 무엇을 구현했는가

기존 준비 자료의 `expected`는 사람이 쓴 정답이었다. 이번에는 정답을 보지 않고 입력에서 답을 계산하는 두 프로그램을 만들었다. B는 명시적인 Python 객체와 일반 조건문으로, C는 RDF 그래프·SHACL 검증·SPARQL 질의로 처리한다. 둘 다 LLM이 아니며 원문에서 관계를 추출하거나 새 관계를 추론하지 않는다.

관계도·검색·검수의 실제 서비스 연결은 없다. 기존 Java 관계 DTO를 직접 읽는 A도 이번 실험에서 실행하지 않았다. B는 기존 서비스가 아니라 비교용으로 새로 만든 공통 모델이다. Java #440의 별칭/최신 상태 수정은 선행 별도 성과다.

## 실행

저장소 루트의 Python 3.11 이상 환경에서 실행한다. 의존성 설치에만 네트워크가 필요하고 실험 자체는 DB·모델·외부 서비스에 접근하지 않는다.

```bash
python -m pip install -e '.[dev,ontology]'
python -m scripts.relationship_semantics_experiment.run \
  --input tests/fixtures/relationship_semantics_cases.json \
  --oracle tests/fixtures/relationship_semantics_cases.json \
  --output /tmp/relationship-semantics-run
python -m scripts.relationship_semantics_experiment.benchmark \
  --input /tmp/relationship-semantics-run/inputs.json \
  --output /tmp/relationship-semantics-benchmark.json
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest --noconftest -q \
  tests/test_relationship_semantics_input.py \
  tests/test_relationship_semantics_experiment.py \
  tests/test_relationship_semantics_rdf.py \
  tests/test_relationship_semantics_lifecycle.py \
  tests/test_relationship_semantics_benchmark.py \
  tests/test_relationship_semantics_extensions.py
```

출력 경로는 새 경로여야 한다. 재실행은 다른 경로를 지정한다. `--oracle`을 생략하면 실행 결과만 만들고 채점하지 않는다. `inputs.json`은 정답이 제거된 재실행 가능한 입력이다. `actual-B.json`, `actual-C.json`에 검증·실제 결과·왕복 결과를 먼저 저장하고 나서 `evaluation.json`을 만든다. `case-01.ttl` 등은 실제 C 입력 그래프다. `summary.json`은 입력 hash·코드 SHA·소스 트리 hash·환경과 통과 여부를 기록한다.

기본 구현과 기능 추가를 구분하려면 기본값 `extended=False`를 사용한다. 확장 구현의 일반 10사례 회귀는 위 실행에 `--extended`를 더한다. 추가 2사례·7질의는 아래처럼 실행한다.

```bash
python -m scripts.relationship_semantics_experiment.run \
  --input tests/fixtures/relationship_semantics_extension_cases.json \
  --oracle tests/fixtures/relationship_semantics_extension_cases.json \
  --extended --output /tmp/relationship-semantics-extension
```

## Python 모델, RDF, SHACL, SPARQL의 역할

| 도구/개념 | 이 실험에서 하는 일 | 하지 않는 일 |
| --- | --- | --- |
| Python 데이터 모델 | 인물, 주장, 관계, 출처, 조회를 명시적인 객체로 보관 | 학습하거나 자연어 의미를 추측하지 않음 |
| B의 검증/조회 코드 | 참조·시간·상태를 검사하고 객체를 필터링 | 기존 Java 서비스의 실행 결과를 대체하지 않음 |
| RDF | 주어·속성·값의 연결로 같은 정보를 표현 | 그래프 DB를 요구하거나 자동으로 과거를 복원하지 않음 |
| SHACL | RDF에 필수 값·허용 값·참조·시간 등 제약을 선언하고 위반 위치/이유를 반환 | 원문이 실제로 참인지 판정하지 않음 |
| SPARQL | RDF에서 조건에 맞는 관계와 근거를 조회 | 이번 구현에서는 새로운 관계를 추론하지 않음 |

예를 들어 `도윤 → 서연: 신뢰` 한 줄만 만들면 상태·출처·시점을 잃는다. C는 `관계 f1 → 관계 종류 TRUST`, `f1 → 근거 주장 c1`, `c1 → 원문 출처 s1`처럼 관계 기록 자체에도 연결을 둔다. `f1 → 상태 ENDED`와 `f1 → 유효성 RETRACTED`, `삭제 기록 → 해당 관계 slot`은 서로 다른 정보다. RDF Collection은 입력 배열 순서까지 왕복하기 위한 표현 비용이며 모든 RDF 모델이 같은 크기를 갖는 것은 아니다.

SHACL의 간단한 예는 ‘관계 상태는 반드시 하나이고 ACTIVE 또는 ENDED여야 한다’는 제약이다. 실제 shape에는 인물 참조·자료형·근거의 회차·종료 근거 유무 등도 들어 있다. 어떤 규칙을 적용할지는 우리가 설계하고, pySHACL이 그 선언을 실행한다. 일반 코드에도 동일한 도메인 규칙을 작성했으므로 SHACL을 썼다는 사실만으로 검증 능력이 더 높아진 것은 아니다.

## 데이터 흐름과 책임

```text
JSON → input.py의 구조 파싱 (정답 값 미접근)
        ├─ TypedModel → 일반 Python 도메인 검사 → 일반 Python 조회
        └─ RdfModel   → RDF 매핑 → SHACL → SPARQL → 공통 결과
                                                        ↓
                          실제 결과 파일 저장 → 별도 평가기 → 수작업 정답 비교
```

- 공통 파서는 엄격한 스칼라 자료형·양수·지원 enum·필드 형식을 검사한다. 잘못된 참조·작품 경계·상태/근거 일치는 B/C가 각각 검사한다. C가 B의 검증기나 조회 함수를 호출하지 않는다.
- 질의 결과는 전체 slot(작품/출발/도착/축/종류/방향), ACTIVE/ENDED, 유효성, 회차, 근거 claim/source를 포함한다. 포함/제외/보류/미지원을 구분한다. fact ID가 같아도 방향이나 근거가 달라지면 실패한다.
- Case는 각각 독립이다. 여러 사례의 `work-a`, `doyun`, `f1`은 같은 객체가 아니다. C는 사례별 Graph와 case 범위 IRI를 사용한다.
- RDF의 직렬화→새 Graph 파싱→객체 복원에서 모든 지원 필드를 대조한다. 지원하지 않는 입력 필드는 조용히 버리지 않고 거절한다.
- 테스트는 정답 제거/변조/값 접근 차단 상태에서도 실제 결과가 같음을 확인한다. 소비자 예문의 문장 일치는 품질 지표로 사용하지 않는다.

## 조회 우선순위: 새로 명시한 실험 계약

1. 입력 손상은 검증 실패다. 미지원 질의와 구분한다.
2. STORY_TIME는 `STORY_TIME_RECONSTRUCTION_NOT_SUPPORTED`, snapshot 이후 회차는 `QUERY_BEYOND_SNAPSHOT`으로 질의 전체가 미지원이다.
3. 지원 질의에서 각 fact를 미래 지식 → 현재 사용자 삭제 억제 → 기록 철회 → 질의 필터 순서로 제외한다. 삭제 억제와 기록 철회는 현재 snapshot revision의 정책이다.
4. 남는 후보 중 `knownFrom <= cutoff < lastChanged`가 있으면 `HISTORY_NOT_AVAILABLE`로 질의 전체를 미지원 처리한다. 삭제/철회/질의 필터로 이미 제외된 관계 하나 때문에 무관한 과거 질의를 막지는 않는다.
5. 미지원 질의는 facts·보류 목록이 비고 모든 fact를 `UNSUPPORTED_QUERY`로 분류한다. 지원 질의는 cutoff 이하 출처를 가진 HOLD 주장만 보류 목록에 포함한다.
6. VALID+ENDED는 종료된 관계로 반환한다. RETRACTED를 ENDED로 바꾸거나 사용자 삭제를 근거 한 개의 철회로 바꾸지 않는다.

이 중 조건 중첩과 과거 복원 불가의 우선순위는 기존 11질의에 없던 경계이므로 추가 회귀로 고정했다. 실제 제품의 전역 정책 변경을 선언하는 것은 아니다.

## 같은 기능 추가의 비교

`extended=True`는 허용 종류에 `AFFECTION`을 추가하고 조회의 선택 필드 `axis`, `nodeRef`, `perspective`를 지원한다. `nodeRef`와 `perspective`는 함께 지정한다. DIRECTED의 OUTGOING은 출발 인물, INCOMING은 도착 인물을 대조하고 MUTUAL/UNDIRECTED는 양 끝점을 검사하여 한 번 반환한다. AFFECTION이라는 종류만으로 축이나 방향을 고정하지 않는다.

기본 모드에서는 AFFECTION 입력이 검증 실패이고 새 필터는 `QUERY_FILTER_NOT_SUPPORTED`다. 이 전후 경계를 테스트한다. 필터는 fact의 포함 여부에 적용하며 HOLD 목록은 여전히 사례·cutoff 범위다. Claim에는 axis가 없으므로 태도 조회의 보류 목록까지 해당 축으로 좁혔다고 해석하지 않는다.

B는 허용 집합과 필터 함수를, C는 선언된 종류 목록과 기존 SPARQL 질의의 조건을 확장한다. 입력·출력 모델과 평가기를 새로 만들지 않았고 실제 서비스 소비자 변환도 구현하지 않았다. 운영 계약에는 AFFECTION이 이미 있으므로 이번 추가는 제품의 새 기능이 아니라 확장 비용 비교다.

## 수명 회귀의 범위

`scenarios.py`는 최대 64개 합성 사건을 메모리에서 재생하는 시험 입력 생성기다. ADD→UPDATE→미언급→END→UPDATE 재개, 확정 별칭 매핑, RETRACT, SUPPRESS를 제공한다. 회차별 중간 snapshot을 보존하고 그 snapshot을 B/C에 동일하게 제공하여 조회 결과를 검증한다.

이력 복원·별칭 매핑은 이 공유 드라이버의 코드다. RDF가 스스로 원문에서 동일 인물을 찾거나 과거 상태를 추론한 것이 아니다. UPDATE는 근거·변경 회차를 바꾸며, fixture에 없는 label/summary 편집은 평가하지 않는다. 사용자 억제는 현재 revision 기준 모든 회차에 적용한다. 뒤늦은 기록 정정 이후 그 이전 snapshot 복원은 드라이버가 명시적으로 거절한다. Java DB 저장·트랜잭션·동시성·실제 END 선택 정확도는 이 검사 범위가 아니다.

## 측정과 도입 판단

별도 프로세스에서 B/C 실행 순서를 번갈아 실행한다. 공통 파싱, 모델 구성, 검증, 첫 질의 묶음, 반복 질의 묶음의 중앙값을 구분한다. 첫 질의도 이미 명시적 검증을 마친 뒤이며 결과 조립·무결성 guard를 포함한다. 새 프로세스이지 OS 캐시를 비운 조건은 아니다.

크기는 사례별 compact JSON과 Turtle의 UTF-8 byte 및 gzip 크기를 함께 기록한다. 온톨로지/shape/query 파일 크기는 데이터 크기에서 제외한다. 메모리는 라이브러리 import를 포함한 전체 프로세스 최대 RSS다. 순수 객체 메모리나 운영 DB 용량으로 해석하지 않는다.

같은 결과를 낸다는 것은 의미 보존의 확인이다. RDF 채택에는 같은 새 종류·질의 추가에서 얻은 재사용 이점, 변환과 의존성 비용, 실제 제품 adapter의 가능성까지 필요하다. B가 충분하거나 A 재사용이 더 단순하면 RDF를 채택하지 않는 결론도 유효하다.

## 읽는 순서

1. `input.py`: 어떤 정보를 명시적으로 구분하는지 확인한다.
2. `typed_model.py`: 일반 객체를 어떤 규칙으로 검사하고 조회하는지 읽는다.
3. `ontology.ttl`, `shapes.ttl`, `queries/`: 같은 정보와 규칙을 RDF·SHACL·SPARQL로 어떻게 표현했는지 비교한다.
4. `rdf_model.py`: JSON/RDF 매핑과 실제 표준 도구 실행 경계를 확인한다.
5. `evaluate.py`, `run.py`: 실행과 정답 비교가 분리되어 있는지 확인한다.
6. 비교 보고서: 측정 조건·실제 결과·한계를 함께 읽는다.

## 공식 자료

- [RDF 1.1 Concepts](https://www.w3.org/TR/rdf11-concepts/): 그래프 데이터 모델.
- [SHACL](https://www.w3.org/TR/shacl/): RDF 그래프 제약 검증.
- [SPARQL 1.1 Query](https://www.w3.org/TR/sparql11-query/): 그래프 질의 언어.
- [RDFLib](https://rdflib.readthedocs.io/): 로컬 그래프 구성·직렬화·질의 구현체.
- [pySHACL](https://github.com/RDFLib/pySHACL): SHACL 검증 구현체.
