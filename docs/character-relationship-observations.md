# 독립 관계 추출과 회차별 관계도

[Java #323](https://github.com/catchhole-soma/catchhole-backend-java/issues/323)은 독립 추출 실험
[AI #93](https://github.com/catchhole-soma/catchhole-backend-ai/issues/93)을 Worker·저장·화면으로 연결한다.
캐릭터·세계관의 기존 추출/비교 모델·prompt·schema는 유지한다. 캐릭터 호출에 관계 배열을
추가하던 과거 방향은 이번 최신 main 기반 구현에 포함하지 않는다.
이전 실험은 [#93 보존 기록](ai-logic-versions/branches/gh-93/README.md), 현재 구현과 검증은
[v0028](ai-logic-versions/v0028.md), Java/프론트 공통 API는 [회차별 계약](episode-relationships.md)을 따른다.

## 호출 흐름

Worker가 `supportsRelationshipAnalysis: true`를 광고하고 Spring이 정수 v1 계약을 반환한
EPISODE/SETTING_EXTRACTION에만 적용한다. 계약이 없는 구 서버·설정집·재비교 작업에는 붙이지 않는다.
청크 하나의 캐릭터·세계관·관계 1차 추출을 함께 실행한다. 관계는 `gpt-6.1-terra/medium`,
입력 상한 64k·출력 상한 16k이며 같은 전역 LLM semaphore와 사용량 원장을 사용한다.
관계 호출의 metering 재시도는 0회다. 공통 캐릭터·세계관의 설정은 변경하지 않는다.

관계 1차가 끝나면 Spring이 관련 기존 관계와 과거 내부 보류만 담은 비교 문맥을 동결한다.
관계 2차도 Terra/medium이며 기존 세계관 비교와 병렬로 실행한다. 캐릭터 비교와 세계관
준비 사이의 기존 순서는 유지하므로 모든 2차 단계를 동시에 실행하는 구조는 아니다.
저장 결과의 공개는 핵심 설정 반영 뒤 Spring 완료 트랜잭션이 담당한다. Python은 관계 DB를 직접 쓰지 않는다.

## 근거와 상태

1차 `relationship-source-spans:v1`은 원문을 결정론적으로 나눈 조각 ID를 고른다.
인용문과 Unicode code point 위치는 서버가 원문에서 복원한다. 회차 전체 offset은 Worker가
청크 시작 위치를 더해 만든다. 이름/지칭의 실제 존재, 인용 범위·중복·상한도 검증한다.
이 방식은 복사 오류를 막지만 문맥 해석이나 실제 인물 해소의 정확도를 보장하지 않는다.

2차 `relationship-comparison:v1`은 `ADD/MERGE/UPDATE/END/EXCLUDE/HOLD`를 반환한다.
관계 종류와 방향, 현재/과거/가정, 모든 신규 관찰의 처리, 기존 참조·중복 쓰기를 검증한다.
한 관찰이 친구 관계 종료와 적대 관계 추가를 함께 뒷받침할 수 있다. 단순 동시 등장이나
도움을 친구·쌍방 감정으로 확대하지 않는다. 관찰이 없는 회차는 기존 관계를 종료하지 않는다.

`HOLD`는 내부 상태이며 사용자에게 검토 숙제를 만들지 않는다. 관련 새 관찰과 함께 재검토할
수 있지만, 이름 없는 1인칭·대명사·역할을 실제 인물에 연결하는 독립 근거는 현재 없다.
`name=null`을 주인공으로 추정하지 않는다. 반면 원문에 이름이 명시된 프로필 미등록 인물은
독립 노드로 만들 수 있다. 이 구분 때문에 일부 명확한 행동도 현재 그래프에 표시되지 않는다.

PREPARED 재시도는 1차를 다시 호출하지 않고, COMPLETED/FAILED 재시도는 관계 모델을 호출하지 않는다.
관계 출력/의미 검증 실패는 관계 FAILED로 남기고 핵심 설정은 보존한다. 인증·quota·lease·
고정 원문 충돌·Spring 저장 장애·예상하지 못한 오류를 관계 실패로 삼켜서 성공 처리하지 않는다.

## 검증과 한계

관계 전용 실제 1~4화 실행은 추출 4회와 비교 4회로 제한했다. 18관찰의 원문 검증은 모두
통과했지만, 이름만 연결하는 비교 실험의 최종 결과는 관계 2개·내부 보류 13관찰이다.
비교 실험은 합성 이름 매핑을 사용했으므로 실제 Java 인물 식별·DB 통합 품질 측정이 아니다.
친구·연인·쌍방 감정의 과잉 생성은 수동 전건 검토에서 찾지 못했다. 회상 1건의 시점 오분류와
3관찰의 선택 인용 문맥 부족, 일부 상호작용 축약, 이름 없는 인물 연결은 남아 있다.
실제 비교 입력에는 기존 관계가 없어 MERGE/UPDATE/END는 자동 회귀만 확인했다.
긴 이름/짧은 이름 연결도 별도 해결 과제이며 공식 관계 Gold·precision/recall/F1은 없다.

`scripts/evaluate_relationship_extraction.py`는 관계 1차만 재현하는 비공개 CLI다.
`--execute` 없이 준비하고, 실제 실행은 별도 새 폴더와 양수 `--max-requests`가 필요하다.
실행 조건·원문/코드 hash·사용량·원시 응답은 0700 폴더/0600 파일로 보관한다.
인증값·provider 오류 본문은 기록하지 않는다. 기존 캐릭터·세계관 Gold는 다시 호출하지 않았다.
