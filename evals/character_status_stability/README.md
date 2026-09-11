# 캐릭터 STATUS 순차 안정성 평가

한 회차를 분석·확정한 뒤 그 결과를 다음 회차 문맥으로 사용하는 실제 서비스 경로를 재현한다.
평가 정답이나 기대 key는 Worker에 전달하지 않는다. 최종 상태가 우연히 맞은 실행만 선별하지 않고
고정된 버전의 모든 시도를 기록한다.

## 실행 환경

- 원격 main에서 분리한 Java/AI checkout, Java 21, AI 개발 의존성, Docker를 사용한다.
- `compose.yaml`은 평가 전용 PostgreSQL/Redis를 localhost:25432/26379에 만든다.
- Java는 e2e profile, 실제 Flyway migration과 JPA validate를 사용한다. 기존 개발 DB를 연결하지 않는다.
- Java 원문 저장소만 로컬 파일로 바꾸고, Python도 Java가 실제 업로드한 파일을 읽는다.
- 세계관 추출은 빈 결과 adapter를 사용한다. 캐릭터 청킹·추출·주체 해소·중복 제거·SQLAlchemy 저장·
  Spring 비교·확정·토큰 정산·heartbeat·checkpoint는 production 구현을 사용한다.

```sh
docker compose -f evals/character_status_stability/compose.yaml up -d --wait
```

Java checkout에서 다음 환경으로 서버를 실행한다. 아래 자격값은 이 평가 컨테이너만의 로컬 fixture다.

```sh
JAVA_TOOL_OPTIONS='-Dspring.devtools.restart.enabled=false' \
SPRING_PROFILES_ACTIVE=e2e \
SPRING_DOCKER_COMPOSE_ENABLED=false \
SPRING_DATASOURCE_URL=jdbc:postgresql://127.0.0.1:25432/catchhole_status_eval \
SPRING_DATASOURCE_USERNAME=status_eval \
SPRING_DATASOURCE_PASSWORD=local-status-eval-only \
SPRING_DATA_REDIS_HOST=127.0.0.1 SPRING_DATA_REDIS_PORT=26379 \
CATCHHOLE_E2E_STORAGE_ROOT=/tmp/catchhole-gh126-eval-storage \
SERVER_ADDRESS=127.0.0.1 SERVER_PORT=18086 \
AI_TOKEN_DEFAULT_GRANT=20000000 \
WORK_PURGE_SCHEDULING_ENABLED=false \
MEMBER_WITHDRAWAL_SCHEDULING_ENABLED=false \
EPISODE_SOURCE_PURGE_SCHEDULING_ENABLED=false \
./gradlew bootRun
```

AI checkout에서 실행한다. 원문 파일은 `001화 제목.txt`부터 `005화 제목.txt` 형식이어야 한다.
`--key-env-file`은 `LLM_API_KEY`만 읽으며 로컬 .env의 다른 모델·DB 설정을 가져오지 않는다.
원문은 기존 OpenAI Responses API(`store=false`)로 전송되므로 원문 전송 권한이 있는 환경에서 실행한다.

```sh
python -m evals.character_status_stability.runner \
  --source-dir /path/to/episode-texts \
  --java-repo /path/to/java-checkout \
  --key-env-file /path/to/private.env \
  --phase baseline --runs 3
```

추출은 `gpt-5.6-terra`, 주체 해소·비교는 `gpt-5.6-luna`, reasoning은 `none`으로 고정한다.
명시적 reasoning 실험은 `--reasoning-effort`로 변경하고 별도 phase/artifact에 기록한다.
각 trial은 빈 새 작품에서 시작한다. 후보 그룹은 아래 고정 확정 정책을 적용한 뒤 다음 화로 진행한다.
API 오류는 그대로 기록한다. 재비교 필요 응답은 아래의 제한된 재확정 흐름으로 처리한다.

## 확정 정책 v4

manifest의 `reviewPolicy`는 `frontend-default-group-confirm-with-history-only-and-recomparison-v4`다.
후보의 인물 이름·설정 key·정답지와 무관하게 현재 프론트의 기본 반영 방식을 적용한다.

- 비교가 `COMPLETED`인 `ADD/UPDATE/MERGE/REMOVE`는 `APPLY_PROPOSAL`로 확정한다.
- 비교가 `COMPLETED`인 `HISTORY_ONLY/REVIEW_REQUIRED`는 `HISTORY_ONLY`로 확정한다.
  `REVIEW_REQUIRED`의 원래 판단·원문 근거를 이력에 보존하며 현재 snapshot에는 적용하지 않는다.
  이는 검토 대기 유지가 아니라 이력으로 `CONFIRMED` 처리하는 선택이다.
- `AMBIGUOUS`, `FAILED`, 값 검증 `INVALID`, 미완료 비교나 지원하지 않는 operation은 계속 보류한다.
  새 캐릭터의 설정도 캐릭터 등록·연결 후 비교를 완료해야 확정한다. 발견만 있는 그룹은 바로 확정한다.
- 같은 이름의 모든 검토 대기 후보를 한 `group-confirm` 요청에 넣는다. 보류 사유가 하나라도 있으면
  그룹 전체를 남기며 일부 후보만 확정하지 않는다. Java의 의존성·context·version 검사도 우회하지 않는다.

v4는 `409 SETTING_CANDIDATE_COMPARISON_NOT_READY`에 한해 사용자가 목록을 새로 받고 비교 완료를 기다린 뒤 다시 확정하는 흐름을 재현한다. 실제 `CharacterFactComparisonWorker`로 현재 작품·회차 후보에 속한 숨김 Job을 처리한다. 비교 실패나 다른 API 오류는 재시도하지 않으며, 최대 3번 확정 라운드(각 라운드에서 검토 가능한 그룹별 요청)과 후보 수에 따른 Job 상한을 둔다. 각 확정 시도와 Worker 요청·정산은 `recomparison/attempt-*/job-*/worker`에 보존한다. 프론트는 오류 뒤 목록을 다시 조회하고 진행 중 비교를 poll하며, 사용자에게 재확정이 필요하다. v3 산출물은 이 흐름을 수행하지 않았으므로 그대로 별도 보존한다.

근거는 원본 `CatchHole-Front`의 다음 파일이다(2026-09-06 확인).

- `src/app/components/catchhole/character/character-fact-comparison-policy.ts:29-45`:
  `REVIEW_REQUIRED`는 제안 적용 불가, 이력 저장 가능이며 기본 모드는 `HISTORY_ONLY`다.
- `src/app/components/catchhole/character/CharacterFactComparisonPanel.tsx:335-365`:
  이력 저장 선택을 제공하고 검토 필요 후보는 이력 저장 또는 수정 후 재비교하도록 안내한다.
- `src/app/components/catchhole/characterreview/CharacterSettingReview.tsx:1838-1865`:
  그룹 확정 가능 조건을 검사하고 모든 검토 대기 후보의 반영 방식과 기준 version을 함께 보낸다.

이전 `confirm-actionable-groups-and-preserve-unresolved-review-v2` 실험은 `REVIEW_REQUIRED`가
있으면 그룹 전체를 보류했다. v3는 확정 선택 정책이 다르므로 이전 v2 실험과 성공률을 동일 조건으로
직접 비교할 수 없다. 정책 변경 전 산출물을 다시 쓰거나 성공으로 재분류하지 않고 별도 실행으로 기록한다. v4도 같은 원칙을 따른다.
이력에 저장됐다는 이유만으로 상태 판정을 통과시키지 않는다. 필요한 현재 부상/회복이 이력에만 남거나
검토 대기에 남으면 최종 원문 대조에서 누락으로 판정해야 한다.

## 산출물과 판정

`build/status-stability/<phase>-<run-id>` 아래 입력 hash, 코드 revision/source hash, 모델 설정,
회차별 claim, 추출 후보, 중복 제거 전후, 비교 API 문맥·결정, 확정 요청, snapshot과 Fact 이력,
토큰 사용량을 저장한다. 원고에서 파생된 비공개 평가 자료이므로 커밋하지 않는다.
일반 추출 초안·STATUS 재검토·주체 해소 후 후보를 구분해 보존한다. 각 회차 시작 시 제품과
평가 harness hash가 시작값과 같은지 확인하며, Java 소스를 변경했다면 실행 전에 서버도 재시작한다.
인증 헤더·API key·lease token·로그인 자격값은 산출물에 넣지 않는다.

STATUS 호출은 현재 청크의 원문 단위 `E*` 참조를 선택하고 코드가 해당 인용과 위치를 연결한다.
`status-evidence-source` 산출물에는 실제 모델에 제공한 참조·원문·청크 내 위치를 보존한다.
이전 화 문맥에는 현재 청크의 참조를 부여하지 않는다. 문장 경계로 나누므로 반복 문장의 위치는
구분하지만, 한 문장 안의 여러 변화는 같은 근거 단위를 공유할 수 있다.

프로세스 성공은 실행 경로가 완료됐다는 뜻이며 의미적 정답 통과를 뜻하지 않는다.
manifest의 `semanticVerdict`는 별도 원문 대조가 완료되기 전까지 `PENDING_INDEPENDENT_REVIEW`다.
005화 최종 snapshot만 보고 성공 처리하지 않는다.

- 001~003화에는 발 부상 근거가 없다.
- 004화에는 오른발/발목 손상과 이동 제약이 활성 상태로 남아야 한다.
- 005화에는 초반 부상·악화와 후반 회복 사건을 이력에 보존하고 심한 오른발 부상은 현재값에서 종료한다.
- 포션 투여뿐 아니라 신체 재생과 이후 달리기가 종료 판단의 근거다.
- 원문에 해독 완료가 직접 명시되지 않은 마비독은 발 부상과 분리해 해석을 기록한다.
- 다른 캐릭터, 독립적인 저주·부상, non-STATUS를 잘못 제거하지 않아야 한다.

최종 검증은 수정본을 고정한 독립 전체 실행 10회와 관련 회귀 테스트를 사용한다.
10/10 통과는 표본 검증 결과이며 무오류 보장으로 해석하지 않는다.

진행 중 코드 수정 필요나 의미적 실패를 발견하면 해당 run 디렉터리에 `STOP_AFTER_TRIAL` 빈 파일을
만든다. 현재 1~5화 전체 trial과 사용량 기록을 끝낸 뒤 다음 trial 시작 전에 멈춘다. 이 중단 묶음은
수정 후 새 고정 10회 묶음과 합산하지 않는다. 즉시 프로세스를 끊으면 진행 중 provider 사용량을
받지 못할 수 있으므로 긴급 중단이 필요하지 않을 때 이 경계를 사용한다.

## 정리

### 이름 정리 단계만 재생하는 진단

`python -m evals.character_status_stability.identity_probe --help`로 이전 실행의
`episode-dir`·원문 `source-dir`·key env 파일·Java repo를 지정할 수 있다. 저장 직전 보관 후보를
제품의 이름 정리 함수에 반복 전달한다. 당시 이름 정리 뒤의 자료라는 점을 manifest에 명시하며,
모델에 정답 이름이나 기대 병합 결과를 전달하지 않는다. 원문·후보 hash와 당시 코드를 보관한다.

이 결과는 `build/status-stability-probes/`에 저장되는 별도 진단이며, Java 저장·확정·snapshot을
거치지 않으므로 원문 1~5화 최종 10회 검증으로 세지 않는다. 직접 provider에서 받은 사용량을
기록하므로 Spring 정산량과 분리해서 비용에 합산한다. 각 호출·실패·미관측 사용량도 보존한다.

### 상태 종료 검수만 재생하는 진단

`python -m evals.character_status_stability.lifecycle_probe --help`로 저장된 완료 STATUS batch를 상태 종료 검수에 전달한다. 같은 contextToken의 성공 문맥·완료 결정을 사용하고, 원문·기대값을 새로 넣지 않는다. provider 원시 응답·사용량·코드와 입력 hash를 비공개 산출물에 보존한다. 직접 API 진단이므로 Spring ledger 및 전체 회차 완주와 구분한다.

평가가 끝나면 Java 프로세스를 종료하고 다음 명령으로 평가 컨테이너를 내린다.
리포트와 로컬 원문 저장소 삭제는 별도로 필요할 때 수행한다.

```sh
docker compose -f evals/character_status_stability/compose.yaml down
```

### 추출 관찰 검수

일반 설정 추출과 독립 STATUS 초벌 뒤에 현재 원문 전체를 다시 읽는 관찰 검수를 수행한다. 시작 활성 상태와 초벌 상태마다 후속 기능 회복을 확인하고, 빠진 종료를 정확한 E 근거를 가진 새 후보로 만든다. 원래 발생·악화 후보의 값과 근거는 보존하며 단발성 처치를 지속 STATUS로 오인한 초안만 분리한다. 실제 지속 재생 효과를 이름으로 일괄 제외하지 않는다.

평가 Worker는 `status-reviewed-candidates`에 초벌, `status-observation-review-before/after`에 검수 전후를 각각 보존한다. 이 단계의 provider 호출·재시도도 일반 추출과 같은 lease/예약/정산 경계에 포함한다. 검수 실패는 초벌만 저장하는 방식으로 우회하지 않는다.

추가 합성 원고 `fixtures/semantic_safety_closure`는 기존 발·팔·저주·다른 인물 감염 대조군에 기억·발성·시야의 후속 기능 회복과 단발 처치/지속 재생 효과를 더한다. 기대값 JSON은 사후 독립 검수 전용이고 실행기는 txt 원문만 읽는다.

앞 청크의 주체 해소 후 STATUS는 같은 Job 안에서만 다음 청크 대상 문맥으로 전달한다. 현재 청크의 E 참조와 이전 관찰의 H 참조는 분리하며 새 종료 근거는 현재 E만 사용한다. 같은 문장 단위 안의 발생·종료 순서를 원문 위치로 구분할 수 없는 경우는 아직 자동으로 해결하지 않으며, 근거가 모호한 종료를 강제로 현재 상태에 반영하지 않는다.

관찰 검수 v4는 입력을 바꾸거나 다른 표를 보여 주지 않은 고정 3표를 사용한다. 유효한 세 표를 전부 얻은 뒤 제외·종료 추가에 2표 이상 동의를 요구한다. 기존 종료 보존과 새 종료 후보 추가는 별도이며 새 후보 추가도 2표가 필요하다. 대표 응답 하나의 summary/현재 E 근거를 그대로 사용하고 합의 결과 전체를 재검증한다. 기본적인 청크 추출 호출은 일반1+STATUS1+관찰검수3=5회이며, validation/출력절단 재시도와 주체·비교 호출은 별도다. 이 방식도 같은 모델의 공통 의미 오판을 보장하여 막는 것은 아니므로 원문 반복 검증을 생략하지 않는다.

처치 과정 초안 제외 분류는 `ONE_OFF_TREATMENT_PROCESS`로 제한한다. 실제 치료/처치의 수단·진행·결과인지 먼저 확인하며, 일시적이거나 외부 환경에서 비롯됐거나 독립 지속성이 부족하다는 이유만으로 실제 기능 제약 관찰을 지우지 않는다. 그 관찰을 현재 snapshot에 반영할지, 이력에만 남길지는 후속 비교의 책임이다.

관찰 검수 v6에서는 기존 `active=false` 초벌을 `preserved_end_observations`에 읽기 전용으로 제공하고 코드가 그대로 보존한다. 수정 가능한 초벌만 `draft_observations`와 분류 응답 대상에 포함하며, 두 목록의 D 번호와 모든 T 연결은 원래 순서를 유지한다. 새 종료 추가에 필요한 3표 검수와 2표 동의는 동일하다. 독립 STATUS 추출 v7은 한 후보에 하나의 관찰을 연결하고, 기존 기억의 실제 회복과 새로운 정보를 처음 알게 되는 장면을 구분하도록 한다.

독립 STATUS 추출 v8은 `states[].observations[]`를 원문 순서로 받는다. 모델의 `kind`를 코드가 기존 후보의 active로 변환하고, 같은 청크 발생 선언에는 별도 START를 요구한다. 출력 값·confidence·E 근거는 보존하며 literal E codec을 재사용한다. 관찰 검수 v7은 private kind가 PAST/HYPOTHETICAL인 원형을 `preserved_noncurrent_observations`로 보존하고 현재 target·onset 계산에서 제외한다. 이전부터 존재한 상태의 종료는 현재 known slot이 없어도 추출할 수 있다. 이 구조가 모델의 상태 분류나 origin 선언의 의미적 정확성을 증명하지는 않으므로 원문 대조를 계속한다.

추출 v9는 모든 등장인물과 사망의 지속성을 다시 명시한다. 각 producer state의 원래 index를 관찰과 함께 private group으로 보존하고, 검수 v8은 그 그룹의 발생·종료를 같은 target에 연결한다. 이름만 같은 다른 producer state나 legacy 입력은 임의로 합치지 않는다. 평가기의 `statusObservationKinds`/`statusObservationGroups` 및 prior 배열은 정확한 재생을 위한 감사 sidecar이며 모델 입력·저장 후보의 필드가 아니다.

추출 v10의 JSON STATUS 값은 name만 받으며 END의 첫 근거가 마지막 활성 관찰의 마지막 근거보다 뒤인지 검사한다. 검수 v9는 시작 활성/앞 청크의 마지막 활성에 연결된 현재 초안, 또는 같은 검증 producer group의 END보다 앞선 현재 초안을 `preserved_state_observations`로 보존한다. 이 목록은 분류 응답 대상에서 제외하고 종료 판단의 원래 D 연결은 유지한다. 기존 부상의 실제 호전 이력을 별도 치료 효과로 오인해 삭제하지 않기 위한 경계다. 새로운 독립 처치 효과의 분류와 기존 END/noncurrent의 원형 보존은 계속 적용한다.

추출 v11은 STATUS 근거 충돌에 한해 직전 두 관찰의 검증된 kind/E 참조를 수정 문맥에 넣는다. 오류 위치만 주고 전체 응답을 다시 생성시키던 재시도에 충돌 대상을 보충하며, 원 응답의 이름·값·인용과 임의 필드는 전달하지 않는다. 성공 경로의 관찰·저장 계약,3시도/한번의출력확장/사용량정산은 유지한다.
