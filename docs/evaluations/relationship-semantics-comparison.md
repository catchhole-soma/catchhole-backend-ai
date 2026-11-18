# 관계 의미 B/C 비교 결과 — Java #442

## 결론과 범위

**3단계 합성 비교는 완료했다. 현시점의 권고는 A(현재 계약) 재사용 가능성을 먼저 확인하고, 공통화가 필요하면 B를 우선하는 것이다. C의 운영 채택 근거는 부족하다.** 두 구현은 일반 10사례·11질의와 확장 2사례·7질의에서 같은 정답·상태·근거를 반환했다. C의 SHACL 선언과 SPARQL 질의가 실제로 실행되지만, 이번 확장에서 B보다 변경이 적거나 비용이 낮다는 결과는 없었다.

이것은 RDF가 일반적으로 부적합하다는 결론이 아니다. 합성 자료의 의미 보존·확장 범위·현재 구현의 비용에 대한 결론이다. 실제 소비자 간 규칙 공유, 다른 RDF 도구와의 교환, 외부 어휘 연계 등 추가 요구가 생기면 표준 도구의 가치가 달라질 수 있다. 이번에는 그런 효익을 측정하지 않았다.

- 추적: [Java #442](https://github.com/catchhole-soma/catchhole-backend-java/issues/442); 선행 [Java #438](https://github.com/catchhole-soma/catchhole-backend-java/issues/438)/[AI #156](https://github.com/catchhole-soma/catchhole-backend-ai/pull/156).
- [실행 안내와 지원 계약](../relationship-semantics-experiment.md), [실행·측정 원자료](relationship-semantics-gh442.json).
- B는 새로 작성한 Python 공통 모델이며 기존 Java 실행 성능 baseline이 아니다. [Java #440](https://github.com/catchhole-soma/catchhole-backend-java/pull/440)의 상태 처리·조회 개선은 별도 성과다.
- 운영 DB/API/UI·LLM 추출/프롬프트·챗봇·검수 연결·추론기는 변경하지 않았다. AI 로직 버전의 상승 대상이 아니다.

## 실행 식별과 실제 정답 비교

| 단계 | 실행 코드 | 조건 | 결과 |
| --- | --- | --- | --- |
| 준비 자료 | `69326a4d8ff714166d3e279d9df05ae97d67a47d` | AI #156 머지 main | 기존 fixture 검사 50개 |
| 초기 기본 구현 | `79d49ab7d61665c1fec4694f147a17fd78e549a8` | extended=false, 10사례/11질의, clean | B/C 검증·왕복·정답 일치 |
| 확장 및 경계 보완 | `fb31dfb26015dd58e62f4231537d3a99eec526a2` | extended=true, 일반 10사례/11질의, clean | B/C 검증·왕복·정답 일치 |
| 동일 확장 코드 | `fb31dfb26015dd58e62f4231537d3a99eec526a2` | extended=true, 추가 2사례/7질의, clean | B/C 검증·왕복·정답 일치 |

- 최종 소스 트리 SHA-256: `489d8947d6478be7fca718e409c813ab0e850ee45f898236926f424cc794e094`.
- 일반 입력 SHA-256: `a427641781b6358881929d194d130daa22e17d09da130b31836c821f4668ac8a`.
- 확장 입력 SHA-256: `d32fb6de66ccb5fb3c68b081976f0de42e3c00ed0e58556f63772159caf43af6`.
- 각 정답 파일 hash, 패키지 버전, 플랫폼, 실제 출력 파일 hash는 원자료에 저장했다. 이후 보고서 커밋은 실험 소스 트리를 바꾸지 않는다.

입력 로더는 정답과 소비자 예문 필드를 허용 목록에서 제외하며 해당 값에 접근하지 않는다. B/C 실제 결과 파일을 먼저 저장하고 그 이후 별도 평가기가 정답 파일을 연다. 입력과 정답의 입력 부분이 다르면 평가를 거절한다. 정답 삭제·변조·값 접근 차단에도 실행 결과가 같음을 테스트했다.

평가는 fact ID뿐 아니라 작품·양 끝점·축·종류·방향·상태·유효성·회차·지지/종료/정정 근거와 source 연결까지 대조한다. 누락·중복·예상 밖 질의·잘못된 제외 사유·금지 slot도 실패한다. 지원되지 않는 STORY_TIME 조회는 미지원이라는 정답과 일치한 것이며, 과거 상태를 복원한 성공이 아니다.

## 구현과 경계 검증

| 검증 | 결과와 입증하는 범위 |
| --- | --- |
| B/C 독립 실행 | B는 일반 객체·조건문, C는 실제 pySHACL·RDFLib SPARQL. C는 B의 검증/조회 함수를 호출하지 않음 |
| 정보 보존 | 지원 필드 전체를 JSON→RDF→Turtle→새 Graph→객체로 왕복; 배열 순서·중복 검사와 사례 ID 격리 |
| 손상 입력 | 없는 참조·중복 ID/slot·작품 혼합·미래 근거·종료 근거 누락·철회 불일치 등 거절 |
| 정답 독립성 | 예상 결과를 제거/변조/접근 차단해도 실제 계산 불변; 저장 결과 변조는 평가 실패 |
| 상태 수명 | 합성 ADD→UPDATE→미언급→END→UPDATE 재개, 확정 별칭, RETRACT, SUPPRESS와 시점 경계 |
| 네트워크 경계 | 소켓 연결을 막고 실제 검증/질의가 동작. 외부 store·remote query·OWL inference·SHACL JS 사용 없음 |
| 기능 확장 | AFFECTION과 방향별 ATTITUDE 7질의, 대칭 1회, 필터 후 history, 제외 우선순위 |

검토 중 두 RDF 경계 결함을 찾아 고쳤다. 첫째, 목록 셀·빈 목록·연결되지 않은 노드의 모르는 속성이 조용히 사라지지 않도록 SHACL을 닫았다. 둘째, RDFLib에서 일반 문자열과 명시적 `xsd:string`의 term 표현이 달라도 같은 값이면 삭제 slot이 매칭되도록 했다. 후자를 고치지 않으면 같은 TRUST 문자열의 표현 차이만으로 억제한 관계가 부활했다. ID·slot 중복과 작품 대조에도 같은 값 비교를 적용했고, 회귀로 고정했다. SHACL 도입만으로 이런 정책 오류가 자동 방지되는 것은 아니다.

수명 회귀의 replay는 **공유 시험 입력 생성기**다. 회차별 snapshot과 확정 별칭을 만들어 B/C에 공급하며 RDF의 독립 추론 능력이 아니다. UPDATE는 근거와 변경 회차를 바꾸며 label/summary 편집을 평가하지 않는다. 현재 snapshot으로 복원 불가능한 과거는 HISTORY_NOT_AVAILABLE, 늦은 정정 이전 replay 복원은 명시적 거절이다. 사용자 삭제는 현재 revision 기준 모든 회차에 적용한다. Java DB 저장/동시성/트랜잭션과 실제 LLM의 END 선택 정확도는 미검증이다.

## 같은 두 기능을 추가했을 때

초기 `79d49ab`에서 확장 `fb31dfb`로 실제 diff를 남겼다. AFFECTION은 기존 제품에 이미 있는 종류를 실험 허용 목록에 추가한 것이다. kind로 axis/direction을 고정하지 않았다.

| 변경 책임 | B | C |
| --- | --- | --- |
| AFFECTION 허용 | 종류 집합에 선택적 추가 | SHACL 확장 어휘와 선택 어댑터; 온톨로지 설명 |
| 축·나가는/들어오는 태도 조회 | 기존 query에 `_matches_filter` 추가 | 기존 fact_decisions/query_status SPARQL에 바인딩 조건 추가 |
| 근거·상태·전체 결과 | 기존 로직 재사용 | 기존 provenance 질의와 결과 매핑 재사용 |
| 공통 입력·평가기 | 변경 없음 | 변경 없음 |
| 실제 서비스 소비자 | 미구현 | 미구현 |

구현 파일의 실제 변경량은 B 1파일 +17/-4줄, C 6파일 +70/-19줄이다. C에는 확장 외에 문자열 동등성 정확성 보완과 설명 주석도 섞여 있으므로 이 수치를 순수 기능 추가 비용이나 유지보수성 점수로 쓰지 않는다. 테스트·fixture·보고서는 이 집계에서 제외했다. 입력·출력·평가기를 함께 재사용한 점은 B/C 모두의 장점이다. C의 질의를 별도 파일로 공유할 수 있지만 실제 두 소비자에 연결하여 절감 효과를 입증하지는 않았다.

기본 모드는 새 종류를 검증 실패, 새 조회 필터를 QUERY_FILTER_NOT_SUPPORTED로 반환한다. 확장 모드에서 DIRECTED는 source/target을 구분하고 대칭 관계는 양 끝에서 한 번 반환한다. HOLD Claim에는 축이 없으므로 확장 조회에서도 HOLD 목록은 사례·cutoff 범위다. 이를 태도별 보류 목록으로 해석하지 않는다.

## 같은 조건의 비용

최종 `fb31dfb`, 일반 10사례·11질의, extended=true. Python 3.12.13, macOS-26.6.2-arm64-arm-64bit, Pydantic 2.14.3 / RDFLib 7.6.0 / pySHACL 0.40.2. 각 방식 새 프로세스 3개, 프로세스마다 전체 질의 묶음을 5회 반복했다. B/C 실행 순서를 번갈아 배치했다. 첫 질의 전 명시적 검증은 완료된 상태다. 반복 값은 프로세스별 중앙값의 중앙값이다.

| 측정 항목 | B: Python | C: RDF |
| --- | ---: | ---: |
| 공통 JSON 파싱 (ms) | 0.855 | 0.572 |
| 방식별 모듈 import (ms) | 0.100 | 51.648 |
| 모델 구성 (ms) | 0.007 | 5.402 |
| 입력 검증 (ms) | 0.103 | 3386.877 |
| 첫 11질의 묶음 (ms) | 0.046 | 70.342 |
| 반복 11질의 묶음 (ms) | 0.020 | 11.444 |
| 직렬화 입력 (bytes) | 17,417 | 60,884 |
| gzip 입력 (bytes) | 7,854 | 11,419 |
| 전체 프로세스 최대 RSS (bytes) | 34,570,240 | 61,177,856 |
| 직렬화 출력 (bytes) | 4,568 | 4,568 |

C의 데이터 그래프는 1122 triples이며 모든 반복에서 출력 hash가 B/C 사이 일치했다. RDF 크기는 Turtle 직렬화 크기이고 그래프 DB 저장 용량이 아니다. gzip은 사례별 압축값의 합이다. 배열 왕복을 위한 RDF Collection과 근거·상태의 명시적인 연결도 크기에 포함한다. 어휘·shape·query 파일 크기는 제외한다.

검증 시간이 큰 이유에는 이 구현의 상세한 SHACL/SPARQL 제약과 작은 그래프마다의 실행 비용이 포함된다. B도 이미 파싱된 객체를 검사하며 공통 Pydantic 비용은 별도 행이다. 첫 질의에는 질의 준비, 반복에는 준비된 질의 재사용이 포함되고 C의 변경 감지 guard도 포함한다. 최대 RSS는 import를 포함한 전체 프로세스 값이다. OS 캐시 초기화·동시성 부하·대용량·서비스 지연은 측정하지 않았다. 이 수치만으로 RDF 기술 전반의 속도나 운영 허용 여부를 결정하지 않는다.

초기 기본 구현의 측정도 원자료에 따로 보존했다. 초기 코드에는 뒤에 발견한 문자열 표현 경계 보완이 없으므로 최종 표와 합치거나 일반적인 성능 개선율로 계산하지 않는다.

## A 재사용 검토와 다음 단계

기존 Java에는 축·종류·방향, ACTIVE/ENDED, 회차·원문 근거, 동일 slot과 별칭 처리, 사용자 정정/삭제가 이미 있다. AFFECTION도 이미 있다. [계약 DTO](https://github.com/catchhole-soma/catchhole-backend-java/blob/9ca148db0c3c5832fdcf8e805ff7f446b549c671/src/main/java/org/monitoring/catchholebackend/domain/relationship/dto/RelationshipGraphContract.java), [slot 규칙](https://github.com/catchhole-soma/catchhole-backend-java/blob/9ca148db0c3c5832fdcf8e805ff7f446b549c671/src/main/java/org/monitoring/catchholebackend/domain/relationship/processor/RelationshipGraphRules.java), [이력 선택](https://github.com/catchhole-soma/catchhole-backend-java/blob/9ca148db0c3c5832fdcf8e805ff7f446b549c671/src/main/java/org/monitoring/catchholebackend/domain/relationship/service/RelationshipGraphSlots.java)을 조사했다.

기존 탐색 API에 전용 axis·incoming/outgoing 필터가 없는 부분은 SQL/API 필터를 추가해서도 해결할 수 있다. 이 경우 페이지 처리·집계 전에 필터해야 한다. 공개 DTO만으로 실험의 Claim curation과 support/end/correction 역할을 전부 복원할 수 있다고 가정하지 않는다. 내부 사건/근거의 읽기 전용 매핑이나 범위가 좁은 adapter 계약이 먼저 필요하다. 실험 RETRACTED/correctionSourceRefs를 기존 사용자 PATCH/DELETE와 동일시하지 않는다.

후속 권고는 실제 소비자 하나와 질문 하나를 고르고 A의 기존 응답에서 필요한 정보와 부족한 정보를 먼저 명시하는 것이다. A로 충분하면 그대로 재사용하고, 여러 소비자의 중복 의미 처리가 확인되면 B 형태의 공통 계약을 검토한다. 이번 결과만으로 RDF 전면 적용이나 가족 추론 단계로 자동 진행하지 않는다. A는 이번 fixture를 실행하지 않았으므로 A의 정답률·속도·효과는 미측정이다.

## 검증 기록과 재현

- 관련 fixture·실행·손상·수명·확장·측정 회귀: **286 passed in 90.24s (0:01:30)**. 기존 자료 50개 + 새 실험 236개다.
- 최종 코드 전체 pytest: **6086 passed, 186 skipped, 1 warning in 127.32s (0:02:07)**. skip은 외부 DB/유료 live 등 별도 조건의 미실행이며 통과로 세지 않는다. warning은 FastAPI/Starlette TestClient의 httpx 사용 deprecation 1개다.
- 변경 Python 14파일 Ruff check/format 통과, `git diff --check` 통과.
- 깨끗한 Python 3.12 환경에서 `pip install -e '.[dev]'`, 이어 `.[dev,ontology]` 설치 및 `pip check` 통과.
- PR/이미지 발행의 테스트 단계 모두 ontology extra를 설치한다. 운영 기본 의존성과 Dockerfile은 유지한다.
- 원자료는 로컬 실험을 기록한다. 최종 PR의 전체 CI 상태는 PR checks에서 별도로 확인한다.

재현 명령은 [실행 안내](../relationship-semantics-experiment.md)를 따른다. 원자료의 git SHA로 체크아웃하고 일반/확장 명령을 `--extended`와 함께 실행한다. 실제 결과를 먼저 저장한 후 채점하며, 새 출력 디렉터리를 사용한다. 준비 자료만 검사한 50개와 실제 B/C를 실행한 검증을 구분한다.
