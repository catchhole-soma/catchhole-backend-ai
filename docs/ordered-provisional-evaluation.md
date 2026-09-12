# 다회차 순차·미확정 누적 평가 실행안 (#180)

이 문서는 원래 요청의 A/B/C/D 조건, 기존 평가 의미, 비용 승인 경계를 함께 보존한다.
현재 구현의 효과를 측정한 정확도 보고서가 아니다. 이 작업에서 실제 모델·semantic judge·
GitHub Actions는 실행하지 않았으며 Notion 정답지를 수정하지 않았다.

## 현재 코드에서 확인한 사실

기존 `ORACLE`은 Gold 1차와 Gold before state를 comparator에 전달한다. `FIXED`는 실제
추출·주체 해소·저장 직전 dedupe/consolidation을 실행하되 회차별 Gold before state를 사용한다.
기존 `ROLLING`은 실제 예측을 `ACCEPT_ALL_PREDICTIONS` 정책으로 누적한다. 후자는 사람의
검수를 재현하지 않으며 새 Java의 journal seal 정책과 같지 않다.

기존 runtime reducer는 개별 `StateApplicationError` 후 계속 진행하고 CHARACTER_DISCOVERY를
알려진 인물로 누적한다. 기존 scorer는 예측을 Gold와 대응시킨 평가용 identity를 사용한다.
이 scorer/reducer를 새 B의 다음 회차 입력 생성에 사용하면 Gold 정보가 흘러들 수 있다.
따라서 기존 세 모드의 의미와 채점 코드는 보존하고 두 실험 모드를 명시적으로 추가했다.

| 조건 | 명시적 모드 / 상태 정책 | 실제 모델 입력의 이전 상태 | 해석 |
| --- | --- | --- | --- |
| A | `COMMON_START` / `COMMON_START` | 모든 회차에 같은 외부 고정 S0 | 사용자 검수 전 공통 시작 상태의 통제 실험 |
| B | `ORDERED_PROVISIONAL` / `VALIDATED_PROVISIONAL` | 실제 Java가 seal한 이전 회차 journal을 S0에 복원 | 새 필터·임시 대상·실패 차단 정책의 실험 |
| C | 기존 `FIXED` / `SCENARIO_LOCAL` | 회차별 Gold before state | 정확한 이전 상태를 제공한 기준 |
| D | 기존 단일 회차 Worker 및 평가 경로 | 기존 확정 설정·기존 prompt/model/reasoning | 다회차 상태가 누출되지 않는 회귀 검증 |

A의 현재 runtime은 호출을 순차 수행해 입력 상태만 통제한다. 운영의 모든 동시 claim,
부분 후보 조회, 네트워크 타이밍 또는 처리량을 재현하는 실험이 아니다. A의 실행 시간을
실제 동시 업로드의 완료 시간으로 해석하지 않는다.

## 새 평가 경계

`runtime_adapter.run_multi_stage_predictions(..., mode=COMMON_START, frozen_start_state=S0)`는
명시적 S0가 없으면 실패한다. 회차별 Gold before state로 대체하지 않으며 Gold state chain
builder 자체를 호출하지 않는다. S0는 deep copy하고 매 회차 동일하게 사용한다. 기존
추출기·주체 해소·comparator와 모델 설정은 그대로 재사용한다. A에 미확정 안내를 넣지 않는다.

새 모드의 `ScenarioPrediction.runtimeStateTrace`에는 실제 입력/출력 EvaluationState projection,
두 상태 hash, 해당 회차 원문 hash, 적용 event ID, 회차 wall time을 기록한다. bundle에는
고정 S0, `runtimePolicyVersion`과 모드를 기록한다. B에는 run/generation, Backend input/output
hash, `SEALED`/`FAILED` readiness도 기록한다. 기존 모드에 이 필드들을 주입하면 거절한다.

새 모드의 scorer는 trace의 실제 상태를 읽으며 Gold before를 대신 넣지 않는다. Gold는
정답·대응 관계·의미 판정을 위한 채점 단계에만 존재한다. 잘못된 상태 hash, 원문 hash,
누락된 B 선행 trace, 입력 chain 불일치, 실패 회차 다음의 관측은 거절한다.

B의 `ordered_runtime_adapter.build_ordered_prediction_bundle`은 Gold 객체를 인자로 받지 않는다.
실제 예측, Java가 내보낸 SEALED journal, 고정 Backend S0, 결정적 domain projector를 받아
검증 후 trace를 만든다. 마지막 회차가 실패하면 그 예측과 실패 건수는 채점 분모에 남기고
출력 상태는 입력 상태로 유지한다. 실패 뒤 회차를 진행하거나 실패를 REVIEW_REQUIRED로
변환하지 않는다. 실행되지 않은 뒤 회차의 모델 결과를 생성한 것처럼 기록하지 않는다.

**B의 현재 live CLI는 의도적으로 실행을 거절한다.** 기존 ROLLING reducer를 새 정책처럼
실행하지 않도록 하기 위한 경계다. 실제 B 모델 실험 전에는 Java Worker API를 사용해
회차별 분석·후보 저장·비교 검증·seal을 완료하는 평가 runner와 아래 domain projector가
연결되어야 한다. 현재 지원하는 B 경로는 실제 saved predictions + sealed journal replay다.
이 준비 상태를 운영과 동등한 실제 B 모델 비교가 완료된 것으로 보고하지 않는다.

### 결정적 journal mirror

`ordered_journal.py`는 아래 Java v1 형식의 읽기 전용 mirror다.

```text
{
  formatVersion: 1, status: SEALED,
  runId, workId, generation, sequence, episodeNo, jobId,
  inputStateHash, outputStateHash,
  changes: [{eventId, path, value, remove, operation, sourceCandidateIds}]
}
```

`sequence`는 0부터 시작한다. state root는 `characters`, `worldSettings`, `references`이며
각 root는 object다. patch는 길이 2 이상의 경로를 사용하고 parent object가 이미 있어야 한다.
REMOVE leaf는 존재해야 하며 SET null은 허용하지 않는다. 같은 event ID와 같은 변경 내용은
한 번만 적용하고 내용이 다르면 실패한다. 배열·회차·회차 내부 이벤트 순서를 보존한다.
run/work/generation이 다르거나 미래 회차·순서 누락·미완성 journal이면 진행하지 않는다.

Hash는 객체 key 정렬, 배열 순서 유지, Unicode 유지 compact JSON, 지수 없는 정규화 숫자를
사용한다. Python 입력은 `load_journal_json`으로 Decimal 정밀도를 보존한다. 큰 Decimal의
정밀도를 잃는 `normalize()` 대신 문자열에서 불필요한 소수점 0만 제거한다. Java의 문자열
정렬과 동일하게 UTF-16 code unit으로 key를 정렬한다.

core mirror는 operation을 비어 있지 않은 문자열로 보존한다. DISCOVERY/CREATE_TARGET 등의
도메인 확장을 임의 거절하지 않는다. ADD/UPDATE/MERGE/REMOVE가 해당 대상·타입·경로에서
유효한지, EXCLUDE/HISTORY_ONLY/REVIEW_REQUIRED가 current state에 섞이지 않는지, 후보 coverage가
완성됐는지는 Java 도메인 validator와 seal의 책임이다. 이 mirror는 raw 1차 후보나 임의의
2차 예측에 SEALED 표시를 붙이지 않는다.

### domain projector 완료 조건

Backend state의 임시 대상 namespace와 실제 ID를 그대로 추적하고, evaluation namespace로
결정적으로 변환해야 한다. 그 변환은 Gold operation/값/미래 인물/미래 회차를 참조하면 안 된다.
채점용 Gold identity 대응이 필요한 경우 runtime trace를 바꾸지 않는 별도 scorer 대응으로
구현하고 모호한 동일 이름을 자동 합치지 않는다. Java exporter의 typed object shape와 실제
ADD/REMOVE/신규 대상 사례에 대한 양쪽 fixture parity가 이 경계의 근거가 되어야 한다.
현재 generic patch replay 테스트만으로 대상·operation의 도메인 동등성을 입증하지 않는다.
`ordered_state_projection.py`는 실제 Java Character/World mapper의 현재 typed shape를 읽고,
실제/임시 namespace·캐릭터 slot·세계관 전체 path·HISTORY_ONLY snapshot을 보존한다.
기존 scorer가 mutation history도 채점하므로 ADD/UPDATE/MERGE/REMOVE history effect는
SEALED journal의 operation/sourceCandidateIds와 실제 frozen 예측을 확인한 뒤 평가 projection에
포함한다. 이는 Backend current state를 변경하지 않는 평가용 이력이다.

`experimental_identity.py`는 채점 단계에서만 opaque Backend ref와 Gold ref를 대응시킨다.
정규화 이름과 도메인의 대응이 양쪽에서 일대일인 경우만 별도 scoring view를 만들며,
같은 이름의 실제 대상이 둘 이상이면 병합하지 않는다. 경로·값·operation은 바꾸지 않고,
raw trace/hash와 다음 모델 입력은 그대로 유지한다. Reference event의 source ID도
Stage1 identity matching이 성공한 경우에만 채점용 Gold event ID에 대응시킨다.

## 데이터 준비 상태와 비용 없는 사전 확인

2026-09-07 로컬 확인 결과, 기존 AI checkout의
`build/eval/multi-stage/private/gold-*.json` 및 GH170 연결 Gold의 Scenario는 DRAFT였다.
`gold-with-states-verified.json`이라는 파일명도 FINAL 증거가 되지 않는다. 저장 ORACLE/FIXED
예측은 존재하지만 이 DRAFT 데이터를 공식 품질 기준으로 새로 평가하지 않았다.
실제 Notion의 현재 FINAL 준비 여부는 별도 read-only 확인이 필요하다.

공식 평가를 준비할 때는 다음 순서로 확인한다. 아래 명령은 실행안이며 이번 작업에서
외부 export 또는 모델 호출을 실행했다는 뜻이 아니다.

```bash
python -m evals.multi_stage_setting.notion_cli \
  --episodes 1,2,3,4,5,6,7,8,9,10 \
  --review-status FINAL \
  --output build/eval/ordered/private/gold-final.json

python -m evals.multi_stage_setting.state_cli \
  --gold build/eval/ordered/private/gold-final.json \
  --output-dir build/eval/ordered/private/gold-states \
  --updated-gold build/eval/ordered/private/gold-with-states.json \
  --mode verified
```

첫 명령은 Notion read-only integration이 필요하다. 두 번째는 포함된 Scenario·1차·2차가
모두 FINAL이 아니면 실패한다. DRAFT를 FINAL로 바꾸어 통과시키지 않는다. 원문 hash,
회차 범위, 시작 상태·seed hash, schema의 canonicalFactType과 빈 값, 연결된 선행 회차를
검증한다. `load_gold_snapshot_v3`와 source/state root loader의 경로 이탈·hash 검사를 재사용한다.
원문·상세 정답·예측·구조화 JSON은 private 경로에만 둔다.

A/B용 `runtime-s0.json`은 해당 실행 시작의 확정 상태를 evaluation schema로 export한
불변 입력이다. C의 회차별 Gold before JSON을 A/B의 다음 입력으로 복사하지 않는다.
과거 회차를 재평가할 때 미래 설정이 포함된 현재 snapshot을 S0로 쓰지 않는다.
지원되는 과거 시점 snapshot이 없으면 그 구간의 실험을 준비 완료로 표시하지 않는다.

## 유료 실행을 승인받은 뒤 사용할 명령

먼저 네 조건에 같은 원문·회차 범위·schema·모델·추론 강도를 고정한다. 기존 로컬 서비스 비교 조건인
추출 `gpt-5.6-terra`, 주체 해소·비교 `gpt-5.6-luna`, 공통 reasoning `none`을 기록하되 실제
비교 실행에서는 승인한 baseline 값이 기준이다. 실험 때문에 모델을 바꾸지 않는다.
다회차 안내문 효과를 별도 비교하려면 B-state-only / B-with-guidance로 나눠 prompt hash를
기록한다. 최근 3화 원문·변경 기록을 추가하는 실험도 별도 조건이며 기본 A/B에 섞지 않는다.

A runtime 명령(유료, 아직 실행하지 않음):

```bash
LLM_REASONING_EFFORT=none python -m evals.multi_stage_setting.runtime_cli \
  --gold build/eval/ordered/private/gold-with-states.json \
  --state-root build/eval/ordered/private/gold-states \
  --source-root private/eval/sources \
  --character-setting-schemas private/eval/character-setting-schemas.json \
  --frozen-start-state build/eval/ordered/private/runtime-s0.json \
  --mode COMMON_START --domains CHARACTER,WORLD \
  --episodes 1,2,3,4,5,6,7,8,9,10 \
  --analysis-model gpt-5.6-terra \
  --subject-resolution-model gpt-5.6-luna \
  --comparison-model gpt-5.6-luna \
  --output build/eval/ordered/private/a-predictions.json
```

C는 같은 명령에서 `--mode FIXED`, output을 `c-predictions.json`으로 바꾸고
`--frozen-start-state`를 제거한다. D는 변경 전후 동일한 단일 회차 입력을 사용한다. 운영의
단일 회차 Worker 테스트와 별개로 C 명령의 `--episodes`를 해당 회차 하나로 고정해 평가하고
실제 prompt/model/reasoning/context와 저장된 예측 채점 결과를 비교한다.

B는 live CLI로 실행할 수 없다. Java validation을 통과한 예측/journal export를 준비한 뒤
Python API `build_ordered_prediction_bundle(...)`에 연결한다. 필수 인자는 실제 예측·원문 hash·
elapsed를 담은 observations, SEALED journals, initial_backend_state, 순수 project_state,
run_id/work_id/generation, `runtime_policy_version=java-journal/v1:<Java revision>`이다.
Gold 인자나 Gold reducer는 없으며 fixture_hash는 채점 파일을 연결하는 메타데이터뿐이다.
정해지지 않은 exporter CLI를 작동하는 명령처럼 문서에 적지 않는다.

저장된 예측을 채점하는 명령은 무료다(`none`을 유지한 경우):

```bash
python -m evals.multi_stage_setting.cli \
  --gold build/eval/ordered/private/gold-with-states.json \
  --state-root build/eval/ordered/private/gold-states \
  --source-root private/eval/sources \
  --predictions build/eval/ordered/private/a-predictions.json \
  --semantic-judge none --quiet \
  --output build/eval/ordered/private/a-report.json

python -m evals.multi_stage_setting.report_cli \
  --report build/eval/ordered/private/a-report.json \
  --markdown-output build/eval/ordered/a-summary.md \
  --json-output build/eval/ordered/a-score.json
```

B/C/D도 실제 prediction 파일을 바꿔 같은 scorer를 사용한다. 의미 판정을 추가할 때는 별도
승인 후 `--semantic-judge openai --judge-model <승인 모델>`을 사용한다. `none` 결과의
`semanticPending`을 오답으로 확정하거나 구조화 JSON·대상 오류를 문구 차이로 덮지 않는다.

### GitHub Actions 입력 준비

기존 `.github/workflows/setting-multi-stage-score.yml`은 ORACLE/FIXED/ROLLING만 선택할 수 있다.
새 A/B를 ROLLING으로 가장해 실행하지 않는다. Actions에는 AI main #64의 평가 기본값(Sol/Terra/Sol·medium)이 통합됐지만 실행하지 않았다. 아래 표는 과거 로컬 모델과 비교하기 위한 명시적 override이며 새 기본값을 뜻하지 않는다. C 실행이 별도로 승인되면 입력은 아래와 같다.

| input | 준비값 |
| --- | --- |
| episodes | `1,2,3,4,5,6,7,8,9,10` |
| mode | `FIXED` |
| domains | `CHARACTER,WORLD` |
| analysis_model | `gpt-5.6-terra` |
| subject_resolution_model | `gpt-5.6-luna` |
| comparison_model | `gpt-5.6-luna` |
| semantic_judge | `false` (judge를 별도로 승인한 경우만 true) |
| confirm_run | 승인 후에만 `RUN` |

Notion 세 data source ID, read-only token, private 원문·schema·state S3 prefix와 OpenAI 인증은
기존 evaluation environment의 설정을 재사용한다. 비밀값을 결과 문서에 적지 않는다.
A/B Actions는 frozen S0 artifact와 journal/export runner 입력이 추가된 뒤 별도 검토 대상이다.

## 예상 호출 범위와 실행 기록

원문과 후보 수가 확정되기 전 고정 호출 수·비용을 약속하지 않는다. 회차 i의 청크 수를 c_i,
주체 해소가 필요한 후보 호출 수를 r_i, 실제 캐릭터 batch 수를 b_c_i, 세계관 주체 batch 수를
b_w_i라고 하면, 양 도메인 live 조건의 1차 호출 기본값은 `2 × sum(c_i)`이고 추가로
`sum(r_i + b_c_i + b_w_i)`가 발생한다. 출력 절단 재시도·schema 재시도·singleton fallback·
stale 재시도는 각각 추가될 수 있다. 캐릭터 batch 크기/입력 상한과 세계관 output 상한은
기존 runtime 규칙을 유지한다. 정확한 범위는 승인 직전 같은 원문으로 청킹하고 이전 saved
prediction의 batch 분포를 집계해 제시한다. 10회차가 항상 10번 호출이라는 가정은 금지한다.

ordered 캐릭터 연결은 이름·별칭 규칙을 먼저 사용한다. 미연결 설정에 추가 앞뒤 청크와 선택
대상이 있을 때만 r_i에 포함되는 LLM 해소가 발생한다. 단일 청크의 미상은 재호출하지 않는다.
따라서 후보가 있는 모든 청크 수를 캐릭터 주체 해소 호출 수로 계산하지 않는다. 규칙 연결과
조건부 호출 모두 신규 발견의 원래 이름·근거 및 실제/임시 인물 구분을 보존해야 한다.

A/B/C 세 live 조건을 모두 실행하면 각각 독립 추출·비교가 필요하며 D와 별도 안내문 실험은
추가 실행이다. 이번 작업에서 이 비용을 승인받았다고 해석하지 않는다. 추정 단가는 현재
승인된 가격표를 확인해 세 `--*-usd-per-million` 옵션을 함께 입력한다. 모델별 단가가 다르면
모든 단계를 단일 단가로 곱한 값은 근사치임을 표시하고 purpose/model별 usage를 따로 집계한다.
재시도·실패에서 발생한 provider input/cached/output도 기존 usage 집계에 포함한다.

실행 manifest에는 두 Git SHA, fixture/schema/S0/source hash, 실행 ID/generation, 회차 범위,
실제 mode/state policy/prompt hash/모델/reasoning, source/context 포함·제외 정책, 실제 token,
비용 산식, 회차별 elapsed, 전체 wall time을 기록한다. A의 직렬 통제 runtime 시간과 운영
작품별 claim 처리량은 서로 다른 측정값으로 표시한다.

## 보고할 지표와 분모

기존 채점식은 바꾸지 않는다. 각 표에는 metric 이름, 정답 조건, 분자/분모, aggregation,
semantic judge 사용 여부를 함께 적는다. 본 문서는 예정된 보고서 형식이며 실제 모델
품질 수치를 생성하지 않았다.

| metric | 정답 조건 / 분자·분모 | aggregation 및 미판정 |
| --- | --- | --- |
| Stage1 candidate P/R/F1 | identity 일치 TP / 예측수 또는 Gold수 | 회차별·도메인별; 원시/저장 handoff/세계관 grouping 건수를 구분 |
| Stage2 operationAccuracy | upstream에 도달한 Gold 중 operation 일치 / upstreamReached | comparator 미응답도 분모에 포함; 도메인별 먼저 표시 |
| target/path/key/removal/temporal/consolidation/valueJson 축 | 해당 축이 적용되는 비교 중 일치 / 해당 축 평가 가능 건수 | 적용 불가와 실패·오류를 구분; case에서 분자/분모 계산 |
| fullDecisionAccuracy | 모든 필요한 축이 일치 / upstreamReached | 의미 미판정이 있으면 전체값 null, resolved와 lower bound 별도 |
| resolvedFullDecisionAccuracy | 결정 가능한 완전 일치 / 결정 가능한 비교수 | semanticPending을 분모에서 제외한 조건부 값임을 명시 |
| harmfulActionRate | 안전한 비반영 Gold에 변이를 제안한 수 / safeNoopCases | 실제 잘못된 상태 반영과 제안 단계 harmful을 구분 |
| afterState P/R/F1 | expected/actual의 현재 값·구조화 값·known 대상·history/held effect 단위 일치 | 기존 reference reducer의 채점 단위 유지; 회차별·도메인별 |
| 최종 10화 상태 | 누락·잘못 남음·중복·잘못된 제거 목록과 건수 | 예측 대상 ID/경로/구조 오류를 표현 차이로 숨기지 않음 |
| runtime failure | stage/errorType별 실패 후보·실패 회차·실행 차단 회차 | 의도적 REVIEW_REQUIRED/EXCLUDE와 분리 |
| token/cost/time | input/cached/output와 실패·재시도 포함 사용량 | 모델/purpose/회차, 전체 wall time; 비교 불가한 타이밍 실험 구분 |
| 오류 전파 | 첫 차이 회차와 영향을 받은 뒤 회차의 journal dependency | Gold가 runtime 입력에 들어가지 않은 trace로 원인 추적 |

`macroAverage`는 도메인 비율의 평균이며 전체 사례를 합친 micro 비율과 같지 않다.
과거 GH170의 70~75%는 캐릭터/세계관 operation accuracy의 평균이었다. 완전 결정 일치율이나
전체 설정의 정답률이라고 부르지 않는다. judge가 꺼졌을 때 규칙 불일치와 의미 미판정을
각각 표시한다. 10화 최종 수치만 제시하지 않고 회차별 최초 차이와 propagation을 확인한다.

## 비용 없는 검증 및 남은 확인

실행한 회귀 검증:

```bash
python -m pytest -q tests/test_multi_stage_setting*.py
```

변경 전 기존 222개 테스트가 통과했다. 저장 synthetic prediction 3개는 AI
`80d5184c006a42248ab214b36d0898c071887900`의 기존 scorer로 만든 full report를 고정했다.
현재 테스트는 ORACLE/FIXED/ROLLING의 모든 집계·분모·회차 state hash·semanticPending·harmful·
실패 진단이 그대로인지 비교한다. fixture는 사용자 원문/유료 결과/Notion 데이터가 아니며
모델 정확도를 입증하지 않는다.

새 테스트는 A에 Gold state builder가 호출되지 않음, 동일 S0 반복, legacy 상태 누출 거절,
실험 trace hash/source/chain 검증, B 실패 분모 보존·뒤 회차 차단, journal 순서·멱등·stale·
foreign/future·nullable patch 거절·숫자/Unicode canonicalization을 다룬다.
Java와 공유하는 `ordered-journal-v1.json`을 Python에서 재적용해 state/canonical JSON/SHA256이
동일한 것을 확인했다(`df2482cd25db7efbf1f410e58698868e734620cab8fade8f88d877e3e18b8424`).
두 회차 synthetic Backend export→sealed replay→typed projection→scorer 테스트에서는 실제
예측의 ADD/REMOVE 이력과 세계관 EXCLUDE를 보존하고 raw trace가 채점 중 바뀌지 않는 것을 확인한다.
후속 실제 Spring HTTP↔Python Worker 통합에서는 무료 fake LLM/S3로 10회차를 처리한 뒤 DB에
저장된 S0와 SEALED journal 10개를 그대로 읽었다. `replay_sealed_journals`가 각 input/output
hash를 검증하고 기존 typed projector가 임시 인물 1명·최종 활성 부상 0개·세계관 색10 상태를
복원했다. 두 원본 근거를 합친 world decision의 sourceCandidateIds도 Java에서 검증했다.
실제 PostgreSQL claim/lease/rollback/미래 정보·무효화 검증은 Java의 별도 DB 테스트 12개로
확인했다. 이 Python 단위 테스트만으로 여러 Worker의 DB 동시성 안전성을 주장하지 않는다.

최종 무료 실행 결과는 Python non-integration 935개 통과, 별도 PostgreSQL fence 12개 통과,
Ruff 전체 검사 통과다. non-integration 실행에서 제외한 integration 14개 중 fence 12개를 따로
실행했으며 나머지 기존 integration 2개는 선택하지 않았다. Java 전체 759개 및 추가 HTTP 통합
3개가 통과했다. `test_legacy_request_golden.py`의 4개 테스트는 변경 전 `80d5184` app의 실제
HTTP 요청을 httpx.MockTransport로 수집한 fixture와 현재 요청을 비교한다. 모델·추론 강도·
system/user prompt·schema·cache·output cap을 포함하는 전체 JSON hash가 동일하다.

남은 유료 실험 준비는 실제 FINAL export 확인, 원문/schema/S0 검증, 운영 평가 입력을 가져올
B live runner/export 연결 및 필요 시 Actions 입력 확장이다. test-only DB export 검증을 유료
평가 runner가 준비됐다는 뜻으로 해석하지 않는다. 실제 A/B 정확도 개선은 아직 측정하지 않았다.

## 2026-09-11: AI main·#65와의 평가 계약 통합

AI main `2dde1839616ecdc584b7f68e5bb33f3f5f181a29`와 #65
`275bada00fcd4d420277d378ddf7f6d417dea899`를 GH180에 통합했다.
ORACLE/FIXED/ROLLING의 모드 의미는 유지하되 최신 upstream의 자료형·상태 의미 채점 변경을
반영했다. 과거 `expected-reports.json`과 예측 입력은 보존했고, 고정 upstream 코드로 같은
입력을 다시 채점한 `expected-reports-pr65.json`을 별도 회귀 기준으로 사용한다. 이 합성
fixture 결과는 실제 소설의 품질 지표가 아니다.

B의 실제 Java v1 journal에는 캐릭터 값의 자료형이 없다. 따라서 시작 상태 projector가
자료형을 제공하지 않으면 `build_ordered_prediction_bundle`의
`initial_character_value_types`에 실제 snapshot/schema에서 내보낸 `fact ref → 자료형`을
제공해야 한다. 시작 상태에 없는 참조, 누락·충돌한 자료형은 거절한다. 이후 회차는 실제
seal된 ADD/UPDATE/MERGE 쓰기와 연결된 1차 후보 자료형만 사용하며 검토·이력 전용·
미완료 후보는 현재 설정의 자료형을 바꾸지 않는다.

이 정보는 평가 DTO에만 보완한다. Backend 원본 journal, 상태 hash, 예측값, 다음 회차에
넘기는 실제 입력은 바꾸지 않으며 Gold·미래 회차·JSON 모양으로 자료형을 추측하지 않는다.
운영과 같은 live B 평가 runner/exporter는 여전히 별도 연결이 필요하다.

통합 검증 범위와 미측정 사유는 [v0004 상세](ai-logic-versions/details/v0004.md)에 기록한다.
