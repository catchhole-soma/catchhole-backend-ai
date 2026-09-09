# AI 로직 버전과 채점 이력

AI 결과에 영향을 주는 변경 PR마다 MD 한 개에 변경 내용·복원할 코드·채점 결과를 남긴다.
작성자는 [TEMPLATE.md](TEMPLATE.md)를 복사하고 아래 목록에 최신 버전을 먼저 추가한다.

## 버전 목록

| 버전 | 기록일 | 변경 요약 | 품질 평가 |
| --- | --- | --- | --- |
| [v0001](v0001.md) | 2026-09-09 | 현재 main과 기존 FIXED 평가를 최초 등록 | 기존 실행 완료·새 기본 모델 조합 미측정 |

## 기본 평가 조건

다단계 평가의 기본은 `FIXED`다. 회차별 Gold 시작 상태를 고정하는 평가 모드이며
`v0001` 같은 로직 버전 번호와는 별개다. 현재 운영값에 맞춰 추출 `gpt-5.6-sol`,
주체 해소 `gpt-5.6-terra`, 비교 `gpt-5.6-sol`, 제품 추론 강도 `medium`을 사용한다.
fallback `LLM_MODEL`은 `gpt-5.6-terra`이며 다단계 judge는 별도 설정의 Sol/medium을 유지한다.
Actions 기본 선택값과 [로컬 실행 예제](../multi-stage-setting-evaluation.md)에 반영한다.
다른 모드·모델로 실험하면 실제 값을 적고, 새 기본값으로 과거 실행 기록을 덮어쓰지 않는다.

## 버전을 올리는 기준

- 추출, 주체 해소, 비교, 정규화·중복 제거·projection, 청킹, 프롬프트, 제품 모델·추론 강도·출력 상한·재시도 등 **결과를 바꿀 수 있는 변경 PR당 전체 로직 버전 하나**를 올린다. 여러 단계가 함께 바뀌어도 하나다.
- main의 마지막 번호에 1을 더해 `v0002`, `v0003`처럼 부여한다. 병렬 PR은 머지 전에 최신 목록과 기준 코드를 확인해 번호 충돌을 해소한다. 기준 코드가 달라지면 재평가하거나 미측정 사유를 갱신한다.
- 같은 버전의 추가 실험은 해당 MD에 실행 단위를 덧붙인다. 이미 기록한 실행·실패를 덮어쓰지 않고 정정은 사유를 남긴다.
- 문서·테스트만 바뀌거나 동작을 보존하는 정리는 버전을 올리지 않고 PR에 사유를 적는다. 채점기·Gold·judge만 바뀌면 제품 로직 버전을 유지하고 같은 MD에 별도 평가를 추가한다.
- 이 번호는 AI 전체 이력의 이름이다. 기존 프롬프트 cache key도 실제 값으로 기록하고, 프롬프트 변경 시 해당 key를 함께 갱신한다.

## PR에 남길 최소 기록

1. 이전 버전, 바뀐 단계·파일, 변경 이유와 전후 동작을 3~5개 항목으로 요약한다.
2. **복원 기준은 전체 Git SHA**로 고정한다. 실행 코드 커밋을 먼저 만들고 그 SHA를 문서 커밋에 기록하면 자기 커밋 SHA를 적는 순환을 피할 수 있다. GitHub 이슈·구현 PR도 연결한다.
3. PR이 squash/rebase되어 SHA가 바뀌면 머지 후 문서 보완으로 main의 최종 복원 SHA를 남긴다. 평가에 실제 사용한 원래 SHA는 보존한다. 로직이 추가되지 않은 SHA 보완은 새 버전을 만들지 않는다.
4. 평가 결과 또는 미측정·실패 사유와 담당자·재평가 시점/조건을 기록하고 PR 체크리스트에 링크한다. 숫자 임계값이나 CI 차단은 이 규칙에 포함하지 않는다.

## 채점 결과를 기록하는 방법

다단계 평가의 집계 `score.json`에서 필요한 수치를 옮긴다. 전체 `summary.md`에는 개별
항목 진단도 있으므로 그대로 복사하지 않는다. Actions artifact는 현재 14일 보관되므로
run URL만 남기지 말고 **실제 조건과 집계 숫자도 MD에 보관**한다.

| 기록 | 필요한 값 |
| --- | --- |
| 실행 근거 | 시각·시간대, run ID/URL 또는 로컬 실행 ID, 실제 실행 명령/입력, 코드 SHA, 채점기 SHA, 변경 파일 없는 커밋에서 실행했는지 |
| 고정 입력 | reportVersion, dataset version·fixtureHash, 회차·시나리오/의존 시나리오 수, 원문 snapshot 식별값/해시와 접근 가능한 비공개 보관 위치 |
| 실행 경계 | mode, domains, stateApplicationPolicy, characterSchemaHash, maxChunks, 청킹 설정과 변경한 실행 제한 |
| 모델·프롬프트 | 실제 추출/주체 해소/비교 모델·제품 reasoning, promptVersions, judge 사용 여부·모델·reasoning·cache 버전, worldSettingNamePolicy |

보고서에 없는 Git SHA·judge 모델/추론 강도 등은 Actions 입력이나 실행 명령에서 보완한다.
기본값을 실제 실행값으로 추측하지 않는다. 환경변수·CLI fallback으로 결정된 값도 확인하며,
추적하지 못한 값은 `확인 불가`와 사유를 적고 비교 가능한 기준 결과로 취급하지 않는다.
원문·Gold·시작 상태·예측의 snapshot은 승인된 비공개 저장소에 보관해 해시로 연결한다.
보관본이 없거나 만료됐다면 재현 불가로 표시한다. 비밀값과 원문을 MD/명령에 넣지 않는다.

다단계 점수는 도메인별로 아래 값을 기록한다. 비율은 원본의 0~1 단위를 유지한다.

| 지표 | score.json 경로 |
| --- | --- |
| 1차 후보 precision / recall / F1 | `stages.{character,world}.stage1.metrics.candidatePrecision / candidateRecall / candidateF1` |
| 1차 값 정확도 | `stages.{character,world}.stage1.metrics.valueAccuracy` |
| 2차 처리 / 전체 결정 정확도 | `stages.{character,world}.stage2.metrics.operationAccuracy / fullDecisionAccuracy` |
| 1차 정답·예측 / 2차 정답·채점 수 | `stages.{character,world}.stage1.counts.gold / predictions`, `stage2.counts.gold / reachedAndCompared` |
| 최종 상태 F1 | `endToEnd.domains.{CHARACTER,WORLD}.afterStateF1` |
| 단계별 미판정 수 | `stages.{character,world}.{stage1,stage2}.counts.semanticPending` |
| 최종 상태 미판정 수 | `endToEnd.domains.{CHARACTER,WORLD}.semanticPending` |
| 런타임 실패 / 상태 적용 오류 | `run.runtimeFailures.total` / `endToEnd.counts.stateApplicationErrors` |
| 의존 회차 적용 오류 / 전이 미판정 | `endToEnd.counts.dependencyStateApplicationErrors` / `endToEnd.counts.semanticPendingTransitions` |

- 2차 정확도에는 1차를 통과해 실제 채점된 수와 전체 2차 정답 수를 함께 적는다. 일부 후보의 100%를 전체 파이프라인 정확도로 설명하지 않는다.
- 레거시 `setting_extraction`은 평가기 이름과 그 결과의 지표명·분모를 그대로 기록한다. 위 다단계 지표로 바꾸거나 서로 비교하지 않는다.
- `ORACLE`의 1차 미평가, 제외한 도메인은 `해당 없음`으로 적는다. 미판정으로 생긴 `null`은 그대로 보존하며 `resolved*` 일부 점수로 전체 점수를 대체하지 않는다.
- 보고서가 만들어졌어도 미판정·런타임 실패·상태 적용 오류가 남으면 `부분 완료`와 그 수를 남긴다. 실행이 끝나지 않았으면 `실패`, 실행하지 않았으면 `미측정`이다. 사유와 재평가 계획을 적으면 머지할 수 있다.
- 개선 비교는 바꾸려는 AI 로직/제품 모델 외에 Gold·원문·시작 상태·회차·모드·채점기·judge·실행 제한을 맞춘 두 실행끼리 한다. 다른 조건의 기존 점수는 참고용이다.
- 채점 기준이 바뀌었다면 이전 로직과 새 로직을 같은 새 기준으로 다시 평가한다. 반복 실행은 성공·실패를 모두 기록하고 선택한 한 번의 점수를 반복 검증한 성능처럼 설명하지 않는다.

## 예전 로직으로 복원하기

1. 대상 버전 MD의 복원 SHA와 실행 설정을 확인한다. 필요하면 `git worktree add --detach <별도-경로> <전체-SHA>`로 이전 코드를 따로 열어 현재 코드와 비교한다.
2. 최신 main에서 복구 브랜치를 만들고 해당 로직 변경을 되돌리거나 필요한 수정만 적용한다. 공용 main의 이력을 reset/force push하지 않는다. 관련 없는 후속 변경을 함께 지우지 않도록 diff를 확인한다.
3. Backend API·DB schema·checkpoint·저장 결과의 호환성과 이전 모델/설정의 사용 가능 여부를 확인한다. 코드 복원으로 이미 저장된 분석 결과가 돌아가지는 않으므로 재분석 필요 여부도 적는다.
4. **다음 번호의 새 버전**에 `복원 원본: vNNNN`을 적고 관련 테스트와 동일 조건 평가 결과 또는 재평가 계획을 남긴다.
5. 복구 PR을 main에 머지해 새 SHA의 이미지 발행·Worker 배포 흐름을 사용한다. 현재 배포 workflow는 과거 SHA의 run 재실행을 건너뛰므로 그것을 복구 완료로 취급하지 않는다.

운영 적용은 [Worker 배포 안내](../../deploy/WORKER_EC2_DEPLOYMENT.md)와
[실제 배포 workflow](../../.github/workflows/deploy-worker-ec2.yml)를 함께 확인한다.
실행 중 Job 정리, Spring 호환 확인, 이미지·Compose SHA 일치와 분석/캐릭터 비교/세계관 비교
세 Worker의 적용 여부를 확인한 뒤 새 버전 기록에 배포 결과를 덧붙인다.
