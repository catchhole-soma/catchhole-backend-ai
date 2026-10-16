# 세계관 속성 비교 묶음 — 실행 결과

작업 이슈: [#267](https://github.com/catchhole-soma/catchhole-backend-java/issues/267) · 선행 #251.
[설계 기준](../specs/2026-10-16-world-property-batching.md) · [계약](../../world-setting-property-batching.md) · [실측 상세](../../ai-logic-versions/details/v0017.md).

## 목표와 확정한 구현

같은 원고의 독립 비교를 입력·예상 출력 토큰 한도 안에서 함께 요청한다. 회차당 1회를 요구하지 않는다.
Spring의 내부 batch·후보 membership·검수/저장 단위는 그대로 유지한다.

- 기존 claim/context/complete API로 여러 독립 batch를 미리 읽고 순차 완료할 수 있음을 확인했다. Java 제품 service/DTO/migration 확장은 필요하지 않았다.
- 같은 대상 재등장은 요청 경계로 둔다. 앞선 결과를 서버에 저장한 뒤 새 문맥을 읽으므로 같은 응답 안의 가상 선행 결과 참조와 별도 ordered_world_manuscript_contract 모듈은 추가하지 않는다.
- 입력64k·출력16k를 초기 한도로 유지한다. 입력의 공통 지시·schema·여유와 예상 출력의 추론 여유를 합산한다. 필수 내용을 자르거나 전역 개수 상한을 추가하지 않는다.

## Task 1 — 고정 입력

- [x] 선행 AI #86 / Java #266 병합 main의 별도 worktree를 사용한다.
- [x] 변경 전 테스트와 과거12개 HTTP 요청을 복원·검증한다.
- [x] 1·2·3화 후보/대상과 바바리안6화·AI2화의 이전 DB snapshot을 고정한다.
- [x] 기존 방식 재생36개의 HTTP body가 과거 원 요청 hash와 같은지 검증한다.

## Task 2 — 서버 계약

- [x] 독립 두 대상의 문맥을 먼저 읽은 뒤 원래 토큰으로 순차 완료한다.
- [x] 같은 완료 재전송이 중복 판단 행을 만들지 않는지 검증한다.
- [x] version이 같아도 대상 값이 바뀌면 기존 토큰을 거절하고 새 문맥을 요구하는지 검증한다.
- [x] 기존 server/HTTP/ordered 회귀와 전체 test bootJar를 실행한다.
- 신규 API/DTO 및 별도 Java↔Python wire exporter는 계약 불변으로 추가하지 않는다. Python pipeline의 실제 Worker DTO 검증과 Java 저장/HTTP 계약 테스트를 사용했다.

## Task 3 — 비교 요청과 검증

- [x] 공통 지시와 그룹별 G*/C*/T*/P*를 사용해 요청을 조립한다.
- [x] 입력/출력 예상량 경계, 단독 초과, 참조 혼동, 후보 누락/중복, strict schema를 RED→GREEN으로 검증한다.
- [x] 기존 내부 validator/projection을 재사용하고 검증된 그룹별 결과를 반환한다.
- [x] 같은 대상의 후속 입력은 앞선 저장 후 읽고, 미리 받은 문맥이 달라지면 재생성한다.

## Task 4 — Worker와 복구

- [x] 실제 Worker factory에 묶음 비교기를 연결하고 Settings/env/Compose를 함께 반영한다.
- [x] 정상 그룹을 보존하고 실패 그룹만 기존 복구를 적용한다.
- [x] subject reset 뒤 새 작업을 먼저 재청구하고 기존 결과 캐시를 비운다.
- [x] 수동 ORDERED의 첫 실패 후 저장 중단과 기존 quota/lease/고정 입력 실패 정책을 유지한다.
- [x] 평가기의 지시 차이 경계·절단 복구가 Worker와 일치하도록 회귀 검증한다.

## Task 5 — 측정과 문서

- [x] 동일 운영 모델로 5입력 × 두 방식 × 3회, 총30회 교차 실행한다.
- [x] 공통 채점기로 1·2·3화18개 결과를 채점하고 후속 회차는 정답률 대신 판정 차이를 기록한다.
- [x] 전체 Python4,067통과/132skip, Java2,738통과/236skip 및 bootJar성공을 확인한다.
- [x] v0017 요약/상세/집계, AI AGENTS/README/계약 문서를 갱신한다.
- [x] 초안 AI #88 / Java #268을 #267에 연결했다. 상위 #251은 자동 종료하지 않는다.

독립 리뷰의 Important3건과 최초 API strict-schema 오류는 회귀 테스트로 보완했다.
원래 계획과 실패 기록은 workspace outputs의 private/에 보존했다.
이번에는 실제 업로드·큐·DB 왕복 시간과 live backend의 lease 만료·통신 중단을 새로 측정하지 않았다.
운영 적용과 해당 경로 계측은 후속 검토 범위다.
