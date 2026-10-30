# 신규 회차 검수 기본 사례 평가 — GH314 V1 (2026-10-30)

이 문서는 #314 V1에서 **실제 Terra 모델**로 신규 회차 검수(설정 충돌·오탈자)를 실행한 기본 사례 결과다.
모든 원고는 평가용으로 새로 쓴 합성 자료이며 사례·기대값은 [`tests/fixtures/episode_validation_cases.json`](../tests/fixtures/episode_validation_cases.json)에 있다.
기대값은 사람이 정한 판정 기준이고 **모델 채택 합격선이나 품질 수치 목표가 아니다**(#314 합의: 모델 비교·순위 실험과 "심각 오류 1건 누락 시 채택 보류" 정책은 만들지 않는다).

## 1. 두 종류의 결과를 구분한다

| 구분 | 무엇을 증명하나 | 이번 결과 |
| --- | --- | --- |
| 모델을 대체한 연결 테스트 | Java HTTP·JPA·Flyway·lease·원장·게시 계약과 Python Worker 연결. provider/S3는 합성 | 기존 Backend `ValidationExecutionHttpPostgresIntegrationTest`(#310, 27개)와 AI `tests/test_episode_validation_worker.py` 등. 이번에 새로 실행하지 않았고 **실제 모델 품질의 근거가 아니다** |
| 실제 모델 호출(이 문서) | 같은 Worker가 실제 `gpt-6.1-terra` 출력으로 계약을 통과하는지, 판정이 기대와 맞는지 | 아래 3~5절. **main 그대로는 설정 분기가 한 번도 완료되지 않았고**, 진단 보정(shim)을 건 실행만 판정 결과를 얻었다 |
| fixture 형식 검사 | fixture가 스스로 일관된지(인용 존재·enum·길이) | `tests/test_episode_validation_cases_fixture.py` 5개, 모델·네트워크 호출 없음 |

## 2. 실행 조건

- 코드: AI main `fba2655`(#310 병합, 로직 v0029), Backend B1 `fdb9ff3`(`feat/gh-314-validation-admission`), Front `feat/gh-314-error-report-ui`. 로컬 `local` 프로필, 일회용 PostgreSQL `gh314_sdk_test`, dev S3. 운영 자료·식별자 없음.
- 모델(검수, `app/validation/models.py`·`typo.py`에 고정): 모든 역할 `gpt-6.1-terra`. PLANNER `medium`, RELATED_SELECTION `high`, COVERAGE `high`, JUDGE `medium`, TYPO `low`. QUERY_EMBEDDING 미사용.
- 프롬프트/스키마: 설정 `models.SYSTEM`(버전 id 없음) + strict schema `validation_planner_v1`·`validation_related_selection_v1`·`validation_coverage_v1`·`validation_judge_v1`, projectionVersion 1·clueVersion 1. 오탈자 `app/llm/prompts/typo_check.md` + `typo_check_v1`.
- 추출(기존 설정 구축·release Job): 로컬 AI `.env`의 `LLM_MODEL` fallback으로 실행됐고 DB `analysis_jobs.model_name`은 `gpt-5.6-terra`다(운영 추출 Sol과 다름).
- 실행 예산(로컬 전용, Backend `validation.execution.*`):

| 분기 | model-calls | lookups | seconds | 호출당 input/output | segment/context | page/search |
| --- | --- | --- | --- | --- | --- | --- |
| SETTING | 100 | 200 | 1,800 | 40,000 / 20,000 | 2,000 / 300 | 50 / 10 |
| TYPO | 20 | 20 | 900 | 20,000 / 12,000 | 2,000 / 100 | 50 / 10 |

  역할 cap: PLANNER·RELATED_SELECTION·COVERAGE·JUDGE 각 40회·예약 2,400,000 raw tokens, TYPO 20회·640,000. lease 60s, 회수 2회. 어떤 실행도 cap·시간 예산에 닿지 않았다(최대 JUDGE 14회/report).

명령(키는 실행 셸에서만 주입):

```sh
# Worker (AI worktree, 기존 venv). .env를 source한 뒤 DB·Spring 대상만 일회용 스택으로 덮어쓴다.
PYTHONPATH=. .venv/bin/python scripts/run_validation_worker.py --enable --poll-seconds 3 \
  --backend-url http://127.0.0.1:18080 --worker-id validation-v1 \
  --internal-key-env SPRING_INTERNAL_API_KEY --model-key-env LLM_API_KEY
# 진단 실행: 같은 플래그를 scratchpad 래퍼(diag_validation_worker.py, GH314_SPAN_SHIM=1)로 실행 — 저장소에 커밋하지 않음
```

## 3. main 그대로의 결과 — 설정 분기 0/4 완료

| report | 결과 | 원인 |
| --- | --- | --- |
| 작품 B 2화 1차 | PARTIAL `INVALID_INSPECTION_CONTRACT`(오탈자만 완료·0건) | PLANNER의 모든 `target_spans.source_ref`가 `TSEG1` |
| 작품 B 2화 재시도 | PARTIAL 같은 코드 | 같음(offset·quote는 전부 정확) |
| 작품 A 3화 1차 | FAILED `INVALID_EXACT_SPAN` | PLANNER `TSEG1` + 오탈자 span offset 1~4자 어긋남 |
| 작품 A 3화 재시도 | FAILED `INVALID_EXACT_SPAN` | 같음(새 PLANNER 호출도 `TSEG1`) |

**결함 AI-1(결정적).** 설정 검사 입력에는 대상 원고의 출처 참조 `T1`이 어디에도 없다. Backend `modelSourceCatalogue`는 `T1`을 일부러 빼고, PLANNER/COVERAGE payload는 `segment.ref=TSEG1`만 준다. 모델은 그 ref를 span에 쓰고 `validate_span(source_ref == "T1")`이 거절한다. `except (ValueError, KeyError, TypeError)`가 원인을 `INVALID_INSPECTION_CONTRACT`로 뭉쳐 로그에도 남지 않는다.
재현: 이 문서의 B2 원고로 검수 접수 → Worker 실행 → `validation_jobs.failure_code`. 수정 후보: payload에 `target_source_ref: "T1"`을 명시하거나 시스템 지시에 "대상 원고 span의 source_ref는 T1"을 넣고, 붕괴시키는 except에서 고정 코드(`SEGMENT_COVERAGE_MISSING` 등)를 보존한다.

## 4. 진단 보정(shim)으로 드러난 추가 계약 위반

설정 분기를 끝까지 관찰하려고 scratchpad 래퍼가 **검증 직전에** 아래만 고쳤다. 모델의 판정·사유·등급은 바꾸지 않았다. 모든 보정은 `[diag-shim]` 로그로 셌다.

| # | 위반 | 관측 | 보정 | 성격 |
| --- | --- | --- | --- | --- |
| S1 | span `source_ref=TSEGn` | 보정 실행 14회의 PLANNER·COVERAGE span 전부(63건) | `T1`로 이름만 교체 | 결정적(AI-1) |
| S2 | span offset이 자기 quote와 1~4자 어긋남(quote는 정확) | 오탈자·COVERAGE·JUDGE 25건 | 자기 quote의 가장 가까운 정확한 위치로 재고정 | 빈번(모델 code point 계산 한계) |
| S3 | COVERAGE가 선택되지 않은 catalogue 항목까지 rule로 기록(20 vs 13) | 1회(`RULE_COVERAGE_MISSING`) | 선택 항목 외 rule 제거 | 입력에 전체 catalogue와 선택 settings가 함께 있어 모호 |
| S4 | JUDGE `FOLLOW_UP`인데 `completion` 채움 | 1회 실패(`INVALID_FOLLOW_UP`) 뒤 보정 2건 | outcome/completion 비움 | strict schema가 모든 필드를 요구 |
| S5 | JUDGE `NO_CONFLICT`인데 `impact=REFERENCE` | 1회 실패(`INVALID_DECISION`) 뒤 보정 2건 | impact·impact_reason 비움 | 같음 |
| S6 | lookup intent 형식: `DIRECT`인데 `source_ref` 없음+B ref, `NEIGHBORS`인데 E 대신 B ref | 실패 3회(`UNKNOWN_SOURCE_REFERENCE` 2·`UNKNOWN_EVIDENCE_REFERENCE` 1) | 생성 직후 정규화하도록 넣었지만 이후 해당 형태가 다시 나오지 않아 실제 변환 0(무해한 DIRECT evidence_refs 정리 23건만) | 계획·FOLLOW_UP 의무로 저장되면 같은 report 재시도가 같은 곳에서 반복 실패 |

보정 없는 실행 4회(B2·A3 각 2회)는 설정 분기 4/4 실패, 보정 실행 14회는 COMPLETED 3회(B2 재시도, live e2e 2회)·미완료 11회였다.

보정해도 남은 실패: `INVALID_DECISION` 4회(확인된 원인: JUDGE `completion.required_reads`가 모든 실제 receipt를 포함하지 않음, `NO_CONFLICT`+impact. 판정은 저장 전이라 B2는 재시도로 회복), `UNKNOWN_BASELINE_REFERENCE` 2회(존재하지 않는 B ref의 BASELINE 의무 — 계획에 저장돼 같은 report 재시도가 매번 같은 곳에서 멈춤).
결과적으로 보정 실행에서도 **작품 A 3화의 세 report(원본·교체 2회)는 모두 PARTIAL**(8/11, 10/15, 11/13 check 판정)로 끝나 검토 완료·추출로 가지 못했다. 짧은 원고인 작품 B 2화(보정+재시도)와 live e2e 2화(보정, 2회 모두)만 COMPLETED에 도달했다.
같은 입력 재시도는 성공한 PLANNER·선정·COVERAGE·오탈자 구간·판정을 다시 호출하지 않았다(B2 세대별 호출 2→1→9→8, 마지막 세대는 남은 JUDGE와 COVERAGE만).

## 5. 사례별 기대 vs 실제(보정 실행)

출처: A3 = 원본 3화 report(rev1), A3F = 교체본 rev2·rev3(rev3은 rev2에 한 문장만 더함), B2 = 작품 B 2화.

| caseId | 기대 | 실제 | 판정 |
| --- | --- | --- | --- |
| A3-SET-DESTROYED-SWORD | CONFLICT/SUSPECTED, 심각·주의, 양쪽 근거 | SUSPECTED_CONFLICT·**SEVERE** 2건(같은 원인 check 둘이 각각 finding). 사유에 2화 파괴·손잡이 매장(기준)과 3화 뽑기·베기(이번 원고) 양쪽, "복원·대체 미발견" 명시 | TP. 같은 원인 미병합 |
| A3-SET-PALACE-MAGIC-SCOPE | NEEDS_CONTEXT + 앞마당/왕궁 안 사유 | rev1 NO_CONFLICT(사유에 "앞마당이 포함된다면 위반일 수 있지만" 언급), rev2 NO_CONFLICT, rev3 **NEEDS_CONTEXT·SEVERE**("앞마당이 '왕궁 안'에 포함되는지 확정할 수 없다") | 1/3 TP, 2/3 FN(판정 오류: 모호성을 인지하고도 정상 처리) |
| A3-SET-CLOAK-COLOR | finding·참고 | 세 report 모두 NO_CONFLICT("평소 착용 습관이며 다른 망토 금지가 아니다") | FN 3/3. 기대값 자체가 작품 해석 문제라 작가 확인 필요 |
| A3-SET-AGE-ONE-YEAR | finding 없음 | 세 report 모두 NO_CONFLICT("한 해가 지나") | TN |
| A3-TYPO-SPELLING | `중단됬다`→`중단됐다` SPELLING | 일치 | TP |
| A3-TYPO-SPACING | `고를수`→`고를 수` SPACING | 일치 | TP |
| A3-TYPO-DUPLICATE-PARTICLE | `두 사람을을`→`두 사람을` TYPO | `사람을을`→`사람을` TYPO | TP |
| A3-TYPO-INTENTIONAL-REPETITION | 제안 없음 | 제안 없음 | TN |
| A3-TYPO-PROPER-NOUN | 제안 없음 | 제안 없음 | TN |
| A3F-SET-SWORD-REFORGED | finding 없음 | rev2·rev3 모두 NO_CONFLICT("회수·재단조로 서로 다른 시점의 상태") | TN |
| A3F-TYPO-CLEAN | 제안 없음 | rev2·rev3 0건 | TN |
| (계획 외) | — | rev3 NEEDS_CONTEXT·CAUTION "검의 찬 바람이 왕궁 안 마법 사용인지" | FP 1(애매함 과잉) |
| B2-ALL-CLEAN | 두 분기 0건 → 자동 완료 | 12 check 모두 NO_CONFLICT, 오탈자 0 → AUTOMATIC receipt·release Job 1 → APPLIED | TN, 자동 경로 확인 |

요약: 오탈자 3/3 정확(FP 0, 의도 표현·고유명사 보존). 설정 충돌 — 명확한 파괴 사용은 일관되게 잡음(등급 심각), 범위가 애매한 규칙은 1/3만 NEEDS_CONTEXT, 부수 소품 색은 0/3(모델은 "습관≠금지"로 정상 판정), 정상 변화(나이·재단조)는 오탐 없음.
원인 구분(#314 요구): 질문 누락은 없었다(세 planted 항목 모두 질문으로 생성). 검색 누락도 확인되지 않았다(사유가 2화 근거를 정확히 인용). FN은 **판정 단계**에서 생겼다.
같은 원인 질문이 두 check로 남아 finding 2건이 된 것은 `merge_questions`의 키(대상·속성 문자열·시점·조건)가 모델 표현 차이로 갈라진 결과다.

## 6. 사용량·지연 (실제 provider raw tokens, `ai_token_usages`)

| report | 세대 | 호출 | input (cached) | output | 비고 |
| --- | --- | --- | --- | --- | --- |
| B2 2화 rev1 | 4 | 20 | 81,548 (24,750) | 29,815 | 1·2세대 main(PLANNER만), 3·4세대 shim. COMPLETED 세대(4) 95초 |
| A3 원본 rev1 | 8 | 20 | 121,713 (43,128) | 48,895 | UNKNOWN 1(Worker 재시작 중단). PARTIAL |
| A3F rev2 | 1 | 15 | 110,768 (20,436) | 26,171 | PARTIAL, 455초 |
| A3F rev3 | 3 | 18 | 137,181 (25,188) | 27,446 | PARTIAL |
| (진단 재검수·live e2e) | | | 아래 7절 | | |

역할별 평균 호출 시간: PLANNER 52~58초, COVERAGE 70~90초, RELATED_SELECTION 10~68초, JUDGE 9~13초, TYPO 2~28초. 설정 분기 report당 JUDGE 10~14회가 호출 수의 대부분이다.
360~890자 합성 원고에서 report 1건(정상 완료 기준) ≈ 호출 15~20회, input 8만~11만, output 3만 raw tokens였다. 이 수치는 이번 합성 사례의 관측값이며 운영 예산 권장값이 아니다.

## 7. 진단 재검수·live e2e(보정 실행)

| 실행 | 결과 | 호출 | input (cached) | output | 시간 |
| --- | --- | --- | --- | --- | --- |
| 작품 B 2화 확정 뒤 진단 재검수(DIAGNOSTIC_ONLY) | PARTIAL `INVALID_DECISION`, finding 0. 추출 Job 2→2(새 Job 0), 다음 회차 등록 허용 | 8 | 30,812 (9,348) | 14,458 | 188초 |
| Front live e2e 1회차(`등대 검수 검증` 2화) | COMPLETED, 설정 0·오탈자 1 → 원문 유지 → 명시 검토 완료 → APPLIED | — | (계정 정리로 원장 삭제) | — | 테스트 7.4분 |
| Front live e2e 2회차 | COMPLETED 1세대, 설정 check 9개 NO_CONFLICT·오탈자 `됬다` SPELLING 1 → 원문 유지 → EXPLICIT receipt → release 1 → APPLIED(attempt 1) | 14 | 46,673 (20,451) | 15,317 | 검수 228초, 테스트 4.7분 |

live e2e 2회차 계정의 전체 AI 사용량(추출 2회 포함, Terra 환산 1배)은 106,036이었다. 두 번 모두 S1 보정(`TSEG1`→`T1`, 2회차 5건)이 있어야 완료됐다. 즉 Front live spec이 main AI Worker 그대로는 `실제 모델 검사가 두 분기 모두 끝나야 합니다` 단계에서 실패한다.

## 8. 한계와 후속

- main 그대로는 실제 모델로 설정 분기를 완료하지 못한다(AI-1). 수정 전에는 신규 회차 검수를 운영에 켜면 모든 report가 PARTIAL/FAILED로 멈춘다. S2~S6도 실제 모델에서 반복되므로 "정확한 code point 재계산은 서버/Worker가, 모델은 quote만" 같은 계약 완화나 strict schema의 조건부 필드 정리가 필요하다. 수정은 별도 AI 작업으로 추적한다(이 브랜치는 문서·fixture만).
- 사례 수가 적고(설정 4·오탈자 5 + 교체본 2 + 정상 1) 원고가 짧다. 계획 Task 8 표 중 칼집 오추출(정정 계약 공백), 회상 속 나이, 디버프/회복, 동명이인·새 설정, 직접 입력 설정 충돌은 실행하지 않았다(fixture `uncoveredTaskTableRows`).
- 추출 모델은 로컬 fallback(`gpt-5.6-terra`)이라 release 추출 결과는 운영과 다를 수 있다.
