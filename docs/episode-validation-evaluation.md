# 신규 회차 검수 기본 사례 평가 — GH314 V1 (2026-10-30)

이 문서는 #314 V1에서 **실제 Terra 모델**로 신규 회차 검수(설정 충돌·오탈자)를 실행한 기본 사례 결과다.
모든 원고는 평가용으로 새로 쓴 합성 자료이며 사례·기대값은 [`tests/fixtures/episode_validation_cases.json`](../tests/fixtures/episode_validation_cases.json)에 있다.
기대값은 사람이 정한 판정 기준이고 **모델 채택 합격선이나 품질 수치 목표가 아니다**(#314 합의: 모델 비교·순위 실험과 "심각 오류 1건 누락 시 채택 보류" 정책은 만들지 않는다).
3~8절은 수정 전 기록으로 보존한다. #331 수정 뒤 같은 사례를 shim 없이 다시 실행한 결과는 [9절](#9-gh331-수정-후-재실행)에 있다.

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

## 9. GH331 수정 후 재실행

2026-10-30 16:31~17:36, [#331](https://github.com/catchhole-soma/catchhole-backend-java/issues/331) 수정 브랜치(`fix/gh-331-validation-target-ref`)의 Worker를 **`--enable`만으로(진단 래퍼·shim 없음)** 실행했다.
로직 기록은 [v0032](ai-logic-versions/v0032.md)다.

### 9.1 실행 조건

- 코드: AI main `804c897`(#106 포함) 위의 수정 커밋. Backend는 B1이 병합된 main `a45437b`의 run 전용 detached worktree(Flyway V101까지 적용), 일회용 DB `gh314_sdk_test`, Redis, dev S3. 모델·추론 강도·실행 예산은 2절과 같다. 추출은 로컬 `.env`의 fallback(`gpt-5.6-terra`)이다.
- Worker에 frozen RAG(`--readonly-url-env`)와 query embedding을 연결하지 않았다(2절과 같음).
- 재실행 중 새로 드러난 원인을 같은 브랜치에서 고친 뒤 Worker를 다시 띄웠다. 그래서 실행은 코드 세 단계로 나뉜다.

| 단계 | 코드 | 내용 |
| --- | --- | --- |
| R1 | `9f18547` | 이슈 수정 1~5(대상 참조 T1·quote 위치 재계산·조건부 판정·COVERAGE 입력/참조 형식·원인 코드) |
| R2 | `3e246e0` | + frozen RAG가 없으면 VECTOR 조회를 제안·저장하지 않음 |
| R3 | `19c4aa9`→`a5722a9`→`dc99b29` | + PLANNER·JUDGE schema를 보고서 안에서 고정(prompt cache) → JUDGE `target_spans` 되풀이를 질문 span으로 확정 |

R1·R2는 작품 A·B(첫 번째)를, R3은 같은 원고로 새로 만든 작품 A·B(두 번째)를 사용했다. 작품 A의 3화 교체본은 원본 report 뒤 파일 교체 → 재검수(rev2)다.

### 9.2 수정 전/후 — report별

| 사례 | 수정 전: main 그대로 | 수정 전: shim | 수정 후 |
| --- | --- | --- | --- |
| 작품 B 2화 | PARTIAL `INVALID_INSPECTION_CONTRACT` ×2(원인 미기록, 실제 `TSEG1`) | COMPLETED(shim+재시도 4세대) → AUTOMATIC → APPLIED | **R3 COMPLETED 12/12**(2세대), finding 0 → AUTOMATIC → **APPLIED**. R1도 1세대 12/12 COMPLETED였지만 finding 2(아래) → 명시 검토 → APPLIED |
| 작품 A 3화 원본 | FAILED `INVALID_EXACT_SPAN` ×2 | PARTIAL 8/11(`INVALID_DECISION` 등) | **R3 COMPLETED 15/15**(3세대). R1은 PARTIAL 6/16 `VECTOR_NOT_READY` |
| 작품 A 3화 교체본 | 실행 안 함 | PARTIAL 10/15(rev2)·11/13(rev3) | **R3 COMPLETED 18/18**(1세대). R2도 COMPLETED 16/16(1세대) |

수정 후 남은 실패와 원인 코드(모두 Backend `validation_jobs.failure_code`와 Worker 로그에 원인 그대로 남았다):

| 실행 | 원인 코드 · 단계 | 원인 | 조치 |
| --- | --- | --- | --- |
| R1 A3 원본 | `VECTOR_NOT_READY` · `LOOKUP:Q9:VECTOR` | COVERAGE가 16개 중 7개 질문에 VECTOR 의무를 넣었고 Worker에 frozen RAG가 없다. 의무가 계획에 저장돼 같은 report 재시도도 같은 곳에서 멈춘다 | R2: 검색이 없으면 schema가 KEYWORD만 제안하고, 남은 VECTOR 의무는 저장 전에 거절 |
| R3 B2 1세대 | `INVALID_DECISION_TARGET_SPANS` · `JUDGE:Q11` | span 4개 질문의 JUDGE가 `target_spans`를 다르게 되풀이 | `a5722a9`(부분집합 보충) 후 재시도 2세대 COMPLETED |
| R3 A3 1세대 | `INVALID_DECISION_TARGET_SPANS` · `JUDGE:Q5` | 같음(span 2개) | 재시도 |
| R3 A3 2세대 | `INVALID_DECISION_TARGET_SPANS` · `JUDGE:Q11` | span 1개 질문에 원고의 **다른 정확한 문장**을 되풀이(부분집합 아님) | `dc99b29`: 원고의 정확한 인용인지만 확인하고 질문 span으로 확정 → 3세대 COMPLETED(`TARGET_SPANS_REPLACED` 1) |

계약 위반 S1~S6은 수정 후 한 번도 실패 원인이 되지 않았다. 정규화 발생 횟수(Worker 로그):

| 정규화 | 수정 전 shim 관측 | 수정 후 |
| --- | --- | --- |
| `TSEGn`→`T1` (S1) | 63건(전부) | **0건** — 입력의 `target_source_ref`와 schema의 `T1` enum만으로 모델이 T1을 썼다 |
| quote 기준 위치 재계산 (S2) | 25건 | 설정 21건(R1 B2 9·R2 2·R3 B2 7·R3 A3 3), 오탈자 5건(R1 A3 2·R3 A3 3) |
| COVERAGE 비선택 rule (S3) | 1회 실패 | 0건(선택 rule enum) |
| FOLLOW_UP의 판정 필드·NO_CONFLICT의 영향 (S4·S5) | 실패 2회·보정 4건 | 0건(prompt 조건 명시 뒤 모델이 null로 냄) |
| 조회 의무 참조 형식 (S6) | 실패 3회 | 0건 |
| `required_reads` receipt 보충 | 실패 다수 | 0건 |
| JUDGE `target_spans` 확정 | — | 1건(R3 A3 3세대) |

### 9.3 사례별 기대 vs 실제(수정 후)

| caseId | 기대 | R3(최종 코드) | R1·R2 | 판정 |
| --- | --- | --- | --- | --- |
| A3-SET-DESTROYED-SWORD | CONFLICT/SUSPECTED, 심각·주의, 양쪽 근거 | SUSPECTED_CONFLICT·SEVERE **4건**(같은 원인 check 4개). 사유에 2화 파괴·손잡이 매장과 3화 사용, "복원·대체 근거 미발견" | R1 SUSPECTED·SEVERE 1건(6/16 시점) | TP. 같은 원인 미병합(2→4건) |
| A3-SET-PALACE-MAGIC-SCOPE | NEEDS_CONTEXT + 앞마당/왕궁 안 사유 | NO_CONFLICT 3 check(사유에 "앞마당의 법적 범위… 확인되지 않았지만") | R1 미판정 | FN(판정 단계, 수정 전과 같은 경향) |
| A3-SET-CLOAK-COLOR | finding·참고 | NO_CONFLICT 2 check("다른 망토 금지 아님") | — | FN(기대값 자체가 해석 문제) |
| A3-SET-AGE-ONE-YEAR | finding 없음 | NO_CONFLICT | — | TN |
| A3-TYPO-SPELLING/SPACING/DUPLICATE | 각 1건 | `중단됬다`→`중단됐다` SPELLING, `고를수`→`고를 수` SPACING, `사람을을`→`사람을` TYPO | R1 같음 | TP 3/3 |
| A3-TYPO-INTENTIONAL-REPETITION·PROPER-NOUN | 제안 없음 | 제안 없음 | 같음 | TN |
| A3F-SET-SWORD-REFORGED | finding 없음 | NO_CONFLICT | R2 NO_CONFLICT | TN |
| A3F-TYPO-CLEAN | 제안 없음 | 0건 | R2 0건 | TN |
| (계획 외, 교체본) | — | NEEDS_CONTEXT·CAUTION 1건 "검의 찬 바람이 왕궁 금법 대상인지" | R2 없음 | FP 1(수정 전 rev3과 같은 내용) |
| B2-ALL-CLEAN | 두 분기 0건 → 자동 완료 | 12 check NO_CONFLICT·오탈자 0 → AUTOMATIC receipt → release → APPLIED | R1: NEEDS_CONTEXT·REFERENCE 2건(DB_RAW_DISAGREEMENT) | R3 TN·자동 경로 확인. R1 FP 2 |

R1 B2의 2건은 검수 결함이 아니라 **기준 오추출**이다. 그 실행의 1화 추출(로컬 fallback 모델)이 "회색 고양이 보리"를 `보리 profile.eye_color=회색`으로 저장했고, JUDGE가 DB와 원문의 차이를 `DB_RAW_DISAGREEMENT`로 보존했다(같은 원인 check 2개).
R3의 1화 재추출은 `profile.attribute=회색`이라 차이가 없었다. R2 교체본은 AUTOMATIC 검토 완료 뒤 release 추출까지 갔지만 설정 반영은 `HELD_REVIEW`(반영 단계 보류, 검수 밖)였다. R3 교체본은 계획 외 finding 1건 때문에 작가 검토 대기다.

### 9.4 사용량·지연 (실제 provider raw tokens, `ai_token_usages`)

| 실행 · report | 세대 | 호출(JUDGE) | input (cached) | output | 시간 |
| --- | --- | --- | --- | --- | --- |
| R1 B2 | 1 | 16 (12) | 76,163 (0) | 19,640 | 277초 |
| R1 A3 원본 | 1 | 12 (8) | 91,974 (0) | 25,216 | 310초(PARTIAL) |
| R2 A3 교체본 | 1 | 20 (16) | 172,377 (0) | 29,260 | 413초 |
| R3 B2 | 2 | 17 (13) | 78,971 (28,518) | 19,347 | 253초 + 24초 |
| R3 A3 원본 | 3 | 27 (23) | 222,295 (55,996) | 36,267 | 453초 + 약 76초 + 약 18초 |
| R3 A3 교체본 | 1 | 23 (19) | 190,496 (48,483) | 31,883 | 461초 |

비교(수정 전 shim, 6절): B2 20호출·81,548(24,750)/29,815, A3 원본 20호출·121,713(43,128)/48,895(PARTIAL), 교체본 rev2 15호출·110,768(20,436)/26,171(PARTIAL).
R1·R2는 호출마다 receipt·근거 목록을 schema enum으로 넣어 **cached input이 0**이었다(수정 전 JUDGE는 호출당 고정 접두 약 1,584 tokens 캐시). R3에서 PLANNER·JUDGE schema를 고정하자 JUDGE 호출당 약 2,048~2,253 tokens가 다시 캐시됐다.
같은 입력 재시도는 성공한 계획·선정·COVERAGE·오탈자·판정을 다시 호출하지 않았다(R3 A3: 22→4→1호출). 수정 후 A3 원본의 check가 11→15개로 늘어 JUDGE 호출과 input이 늘었다. 이 수치는 합성 사례의 관측값이며 운영 예산 권장값이 아니다.

### 9.5 남은 문제

- 판정 품질: 범위가 애매한 규칙(앞마당 vs 왕궁 안)은 이번에도 NO_CONFLICT였고(FN), 같은 원인 질문이 4개 check로 남아 finding이 4건이 됐다(`merge_questions` 키 분기). 교체본의 계획 외 NEEDS_CONTEXT 1건(FP)도 다시 나왔다. 모두 판정·병합 단계의 의미 문제이며 이번 계약 수정 범위 밖이다.
- JUDGE의 `target_spans`는 서버가 질문 span으로 확정한다. 모델이 되풀이할 필요 없는 값이므로 wire/schema에서 빼는 정리는 Backend와 함께 별도로 판단한다.
- VECTOR는 frozen RAG를 연결한 Worker에서만 제안한다. 연결된 운영에서도 색인 미준비는 기존대로 `VECTOR_NOT_READY`/`VECTOR_INDEX_PARTIAL`이다(이번 실행 범위 밖).
- 기준 추출이 로컬 fallback 모델이라 오추출이 검수 finding으로 이어질 수 있다(R1 B2). 운영 추출 모델로는 재현하지 않았다.
- 사례 수는 8절과 같이 적다. 재실행 때마다 질문 수·문장이 달라 수치를 품질 점수로 쓰지 않는다.
- 재실행 뒤 테스트 작품 4개를 API로 파기했다(purge 4건 COMPLETED, works·episodes·validation_reports·validation_jobs·analysis_jobs·ai_token_usages·후보 0행, dev bucket `works/<id>/` 객체·버전·삭제 표식 0). DB와 테스트 계정은 남겼다.
