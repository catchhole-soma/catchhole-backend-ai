# OWL 세계관 추론 실험 — Java #445

기존 관계 Python/RDF 실험(#442)과 Python/OWL 관계 추론 실험(#444)을 보존하고, 별도 합성 세계관에서 **기존 OWL 2 DL 엔진을 재사용할 가치**를 확인한다. Python 추론기를 새로 확장하지 않는다. 이번 Python 코드는 입력·출처·시점·엔진 호출·근거 설명·평가를 담당한다.

이 코드는 `scripts/world_owl_experiment/` 안의 오프라인 실험이다. 운영 `app/`, 원문 추출 프롬프트, DB/API, 사용자 설정, 챗봇, GraphRAG를 바꾸지 않는다. 실제 작품에서의 추출 정확도·답변 품질·운영 성능은 이번 결과로 판단할 수 없다.

## 기존 데이터에서 달라지는 점

현재 `ExtractedWorldSettingCandidate`는 `category / subject_name / scope_name / setting_name / extracted_value / evidence_spans` 등의 서술형 후보를 전달한다. `extracted_value` 문자열이 저절로 논리식이 되지는 않는다. 이 실험은 **사람이 원문의 의미를 확인해 형식화한 자료**를 새 계약에 넣는다. 기존 세계관 후보를 변환하는 운영 adapter나 자연어→논리식 LLM은 구현하지 않았다.

예를 들어 다음 두 입력은 다르다.

- “모든 마도사는 학자다.” → `Mage ⊑ Scholar` : 마도사이면 학자지만 역방향은 보장하지 않는다.
- “기사이면서 신성 계약자인 사람을 성기사라 정의한다.” → `Paladin ≡ Knight ⊓ SacredContractHolder` : 필요충분조건을 확인했을 때만 양방향 정의로 쓴다.

“보통”, “해서는 안 된다”, “전해진다”는 각각 경향·규범·소문이다. `DEFAULT / NORM / RUMOR`로 보존하되 엄격한 공리에 넣지 않는다. `STRICT + confirmed=true`도 원문의 진실성을 기계적으로 증명하는 값이 아니라 수작업 형식화의 승인 표시다. 같은 형식에 들어맞는다고 의미까지 검증한 것은 아니다.

## 엔진과 역할

- HermiT: OWL 클래스 표현식과 공리의 실제 논리적 함의를 판정한다.
- Owlready2: 이 구현에서는 고정 버전의 bundled HermiT/OWLAPI jar를 제공한다. `sync_reasoner`의 결과를 재해석하지 않고 OWLAPI를 직접 호출한다.
- JPype: Python 프로세스 안에서 JVM을 한 번 시작하고 Java API를 호출한다.
- `contract.py`: 지원하는 표현·참조·타입·출처 해시를 엄격하게 검사한다. SHACL을 실행하는 단계가 아니다.
- `policy.py`: 같은 작품·시간선, 독자 회차, 작중 시점, 저장 revision, 출처 철회와 명제 채택 조건을 판정한다.
- `engine.py`: 선택한 명제를 OWL 공리로 변환하고 실제 `isConsistent / isEntailed / isSatisfiable`을 호출한다. 인물별·직업별 추론 규칙을 직접 구현하지 않는다.
- `proofs.py`: OWL 엔진을 반복 질의해 결론을 지지하는 모든 부분집합 최소 근거 집합을 찾는다. 엔진 내부 증명 트레이스를 받는 기능은 아니다.
- `reasoning.py`: 일관성 확인 → 질의 판정 → 근거 연결 → 실제 RDF/XML 내보내기를 조율한다.
- `run.py / evaluate.py`: 실제 결과를 저장·재확인한 뒤에만 별도 정답을 읽는다.

새 명제를 추가하면 엔진이 의미를 자동 발견하는 것이 아니다. **새로운 세계관 정의는 우리가 선언하고, 그 정의가 기존 정의·사실과 논리적으로 맺는 결과는 범용 엔진이 계산한다.**

## 지원하는 논리와 판정

입력은 OWL 2 DL의 제한된 부분집합이다. 클래스 이름, `and / or / not / some`, 하위 개념, 동치, 배타 클래스, 개체의 클래스 소속, 양/음의 개체 관계 주장을 지원한다. 엔진 자체가 더 넓은 OWL 기능을 제공해도 이 입력 adapter가 제공한다고 주장하지 않는다. 전칭 제한·개수 제한·datatype·nominal·sameAs·서로 다름 선언은 이번 계약에 없다.

| 질의 | 판정 | 의미 |
|---|---|---|
| 개체가 C인가 / 관계가 있는가 | ENTAILED | 적격 지식의 모든 모델에서 해당 명제가 참 |
| 같은 질의 | REFUTED | 해당 명제의 **명시적인 논리적 부정**이 함의됨 |
| 같은 질의 | UNKNOWN | 참도 부정도 함의되지 않음 |
| A가 B의 하위 개념인가 | ENTAILED / UNKNOWN | 포함이 함의되는지 확인. 포함 명제의 부정 판정은 이 adapter에서 지원하지 않음 |
| C가 존재 가능한 개념인가 | SATISFIABLE / UNSATISFIABLE | C에 속하는 것이 있을 수 있는 모델이 존재하는지 확인 |
| 전체 지식이 모순인가 | INCONSISTENT | 일반 질의를 `BLOCKED_INCONSISTENT`로 막고 최소 충돌 집합을 반환 |
| 엔진 실패·한도 초과 | ERROR | UNKNOWN으로 바꾸지 않으며 완료된 설명으로 집계하지 않음 |

열린 세계에서 모름은 부정이 아니다. `Mage ⊑ Scholar`와 `Harin:¬Scholar`가 있으면 `Harin:¬Mage`가 논리적으로 따라올 수 있지만, `Harin:Mage`가 없다는 이유만으로 `Harin:¬Mage`가 되지 않는다. `A ∨ B`는 배타적 선택이 아니며, 한 개체가 둘 다여도 별도 배타 선언이 없으면 모순이 아니다.

불가능한 클래스가 있어도 그 클래스가 비어 있는 모델은 가능하다. 예를 들어 서로 배타적인 두 클래스를 동시에 만족해야 하는 개념은 `UNSATISFIABLE`이지만, 그 개념에 속한다는 실제 주장을 추가하기 전에는 지식 전체가 `CONSISTENT`일 수 있다. 모순된 지식에서 모든 질문을 참이라고 답하는 논리적 폭발을 제품 답변으로 노출하지 않는다.

`∃hasContract.SacredContract`는 조건을 만족하는 대상이 존재함을 뜻한다. 기존 인물/계약 ID 중 하나라고 선택하거나 새 이름을 만들어내지 않는다. OWL은 이름이 다르다고 자동으로 서로 다른 개체라고 가정하지 않는다.

## 원문·시간·철회 계약

각 명제는 공리(`subclass/equivalent/disjoint`) 또는 사실(`classAssertion/propertyAssertion`)과 `sourceIds`를 가진다. 실제 원문은 합성 `sources[].text`에 있고 SHA-256으로 일치 여부를 확인한다. 이 실험은 출처 본문 전체가 짧은 근거 문장이며 운영 chunk/span 조회를 구현하지 않는다.

같은 명제를 지지하는 여러 `sourceIds`는 **OR**다. 한 출처가 남아 있으면 명제는 유지된다. 서로 다른 statement ID가 같은 논리적 사실을 주장하면 각각 독립적인 근거 대안이 될 수 있다. OWL 그래프가 같은 공리를 합쳐도 원본 statement ID 목록을 잃지 않는다.

1. `workId / contextId`: 명제와 출처 모두 질의의 작품·시간선과 일치해야 한다.
2. `cutoffEpisode`: 사실뿐 아니라 **정의의 출처 회차도** 독자가 읽은 범위 안이어야 한다.
3. `storyTime`: `validFrom <= storyTime < validTo`인 명제만 채택한다. 끝이 null이면 열린 구간이다. `scope_name`이라는 표시 경로를 작중 시점으로 해석하지 않는다.
4. `snapshotRevision`: 명제와 출처가 기록됐고 아직 철회되지 않은 저장 revision이어야 한다. `recordedRevision <= revision < withdrawnRevision`이다.
5. `STRICT`이며 확정된 명제만 채택하고, 적격 출처가 하나 이상 있어야 한다.

제외 사유의 우선순위는 작품/시간선 → 아직 기록되지 않음 → 철회 → 작중 시점 → 경향/규범/소문 → 미확정 → 적격 출처 없음이다. 여러 부적격 이유를 모두 합산하는 진단이 아니라 하나의 결정적 이유를 반환한다.

원문 파기(`text=null`, 원래 해시만 유지)는 명제 철회와 다르다. 별도의 철회가 없으면 확인된 명제를 유지하되 인용문은 복원하지 않는다. 이 검증은 새 snapshot에 주어진 null의 보존이며, 과거에 저장한 입력/결과/HTML 사본을 찾아 삭제하는 기능은 아니다. 원문 파기 이후 이 명제를 제품에서 계속 쓸 수 있는지는 운영 권한/파기 정책에 연결해야 하며 이번 합성 정책을 운영 계약으로 승격하지 않는다.

정의 정정은 구 정의의 철회 revision과 새 정의의 기록 revision으로 표현한다. 모든 변경은 **적격 직접 공리로 새 ontology를 만들고 전체 재계산**한다. 과거 파생 결론을 전제로 다시 넣지 않는다. 대체 경로가 있으면 유지되고 마지막 지지가 사라지면 철회되며, 철회된 결론의 반대가 자동으로 참이 되지 않는다. 이는 증분 truth-maintenance 구현이 아니다. 화면에서 숨기는 사용자 억제 정책도 구현하지 않았고 부정 공리로 대신하지 않는다.

## 근거 설명의 보장과 비용

`supports`의 한 항목은 결론을 지지하는 명제 ID의 집합이다. 그 집합의 어떤 명제를 하나 빼도 같은 결론을 도출하지 못하면 **부분집합 최소**다. 전체 중 원소 수가 가장 적은 집합만 반환하는 것이 아니며, 크기가 다른 독립 근거도 모두 반환한다.

알고리즘은 전체 후보에서 불필요한 명제를 하나씩 빼서 최소 집합 M을 찾는다. 다른 최소 집합은 M의 원소 중 하나 이상을 제외해야 하므로 그 분기를 모두 탐색한다. 캐시는 한 snapshot의 엔진 객체 안에만 존재하며, 실제 계산마다 ontology/reasoner를 새로 만든다. JVM 프로세스만 공유한다.

- 각 근거에는 `statementIds / axiomIds / factIds / sourceIds / sourceAlternatives / sources`가 붙는다.
- 서로 다른 증명 경로가 같은 최소 명제 집합을 쓰면 하나의 집합이다. 모든 증명 단계·순서를 열거하는 것은 아니다.
- `supports: [[]]`에 해당하는 빈 명제 집합은 논리 자체만으로 성립하는 결론이다. `supports: []`인 UNKNOWN이나 SATISFIABLE과 다르다.
- 일관성 자체나 SATISFIABLE의 모델 증명서는 출력하지 않는다. UNKNOWN도 “근거가 없다”는 원문 증명이 아니다.
- 명제 16개, 엔진 검사 8,192회/사례, 근거 탐색당 노드 8,192개를 상한으로 둔다. 초과하면 불완전한 근거 목록을 성공으로 반환하지 않는다. 엔진의 개별 작업 timeout도 적용한다. 이는 전체 요청의 강제 프로세스 종료 deadline은 아니다.
- 근거 추출은 경우에 따라 지수적으로 증가한다. 작은 합성 사례에서 완료했다는 사실을 대규모 서비스의 근거 탐색 성능으로 일반화하지 않는다.

## 재현

Java 21과 Python 3.12에서 검증한다. 기본 운영 Python 의존성에는 JVM bridge를 추가하지 않고 `world-owl` extra와 CI 테스트 환경에만 설치한다. Dockerfile과 제품 패키지의 `app*` 범위를 유지한다.

```sh
python -m pip install -e '.[dev,world-owl]'
python -m pip check
python -m pytest tests/test_world_owl_engine.py tests/test_world_owl_experiment.py -q
python -m scripts.world_owl_experiment.run \
  --input tests/fixtures/world_owl_cases.json \
  --oracle tests/fixtures/world_owl_oracle.json \
  --output /tmp/world-owl-run
python -m scripts.world_owl_experiment.benchmark \
  --input tests/fixtures/world_owl_cases.json \
  --output /tmp/world-owl-benchmark.json --repeats 3
```

출력 폴더/benchmark 파일은 새 경로여야 한다. 기존 기록은 덮어쓰지 않는다. 대소문자만 다른 case ID는 운영체제별 파일 충돌을 막기 위해 거절한다. 실제 실행은 입력 파일만 읽고, `inputs.json / actual.json / measurements.json / ontologies/*.owl`을 쓴 다음 저장된 actual/OWL 문서가 원래 결과와 같은지 확인하고 별도 정답을 연다. 정답 판정뿐 아니라 저장된 출처·회차·원문·해시·작품 범위도 원본 입력과 독립 대조한다. `evaluation.json / manifest.json`에 정답 비교와 코드·입력·정답·출력 해시를 남긴다. `.owl`은 채택한 **직접 공리**의 실제 RDF/XML이며 모든 함의의 materialized closure가 아니다. 원문 출처 연결은 JSON에 보존한다.

측정은 세 번의 순차 독립 프로세스에서 수행한다. JVM 시작/표현식 준비, 일관성·질의 판정, 근거 탐색, RDF 내보내기를 분리하며 전체 프로세스 RSS는 Python과 JVM을 합친 값이다. 전후 snapshot 모두 전체 재계산이다. 기존 B/C의 다른 입력·엔진·측정 방식과 숫자를 직접 비교하지 않는다.

## 읽는 순서와 결과

먼저 [합성 입력](../tests/fixtures/world_owl_cases.json)과 [독립 정답](../tests/fixtures/world_owl_oracle.json)을 보고, [입력 계약](../scripts/world_owl_experiment/contract.py) → [적격성](../scripts/world_owl_experiment/policy.py) → [OWL bridge](../scripts/world_owl_experiment/engine.py) → [근거 탐색](../scripts/world_owl_experiment/proofs.py) → [전체 흐름](../scripts/world_owl_experiment/reasoning.py)을 읽으면 된다.

실행 수치와 검증 결론은 [평가 보고서](evaluations/world-owl-experiment.md)에 기록한다. 이 실험은 Java #445에서 추적한다. 이전 AI #157/#158은 별도 실험으로 보존하며, 이 PR을 위해 선행 머지가 필요하지 않다.

참고 표준/엔진 문서:

- https://www.w3.org/TR/owl2-primer/
- https://www.w3.org/TR/owl2-direct-semantics/
- https://www.w3.org/TR/owl2-profiles/
- https://www.hermit-reasoner.com/
- https://owlready2.readthedocs.io/en/latest/reasoning.html

표준의 의미와 이 adapter의 지원 범위를 구분한다. 설치된 jar/의존성의 정확한 버전·해시는 실행 manifest/measurement에 기록한다.
