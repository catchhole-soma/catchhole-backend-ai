# 챗봇 평가 데이터와 저장 결과 채점 v1 (#242)

이 단계는 합성 평가 자료와 **저장된 실행 결과를 검토·비교하는 파일 전용 도구**다. 현재 제품의 정답률을 측정하거나 베타 공개를 승인하는 단계까지 끝난 것은 아니다.

## 준비한 것과 남은 것

| 범위 | 상태 |
| --- | --- |
| 합성 근거 72개·질문 26개·필수 사실 70개·금지 단정·정상 보류 기준 | 내부 검토 초안 |
| 데이터/근거 참조·범위·버전·hash 검사, 정답 없는 실행 입력 내보내기 | 구현 |
| 저장 결과 채점, 세 방식의 동일 조건 비교, JSON/Markdown 집계 | 구현 |
| 실제 Python QA 실행 → 결과 포착 → 채점 | 고정 모델/Spring 대역 2문항 확인 |
| 26문항 × 3방식의 전체 실행 및 검색 품질 비교 | 미실행 |
| 실제 provider 정확도·실비·응답 지연 | 미측정 |
| PostgreSQL seed, 세 검색 방식의 실제 실행/결과 포착 adapter | 후속 작업 |
| Java 원장 대조·Front 장애 E2E·운영 베타/중단/복구 절차 | #242 후속 범위 |

이 평가 도구는 초기 초안을 최신 main에 연결한 파일 전용 도구다. 평가 모듈 자체는 제품 handler·프롬프트·모델·운영 플래그를 변경하지 않는다. 같은 변경 묶음의 #247 제품 수정과 평가 도구의 역할을 구분한다. 실제 원고를 사용하는 이번 비교는 아래 합성 26문항과 별도의 비공개 데이터셋이며, 두 결과의 분모를 합치지 않는다.

## 파일과 데이터 경계

- `tests/fixtures/chat-evaluation-v1/dataset.json`: 합성 코퍼스와 질문별 기대 사실/허용 근거 묶음/금지 단정. 실제 원고나 DB dump가 아니다. `CURRENT/HISTORY/DIRECT/CHUNK`는 기존 조회 계약의 구분을 따르되, 검토를 위한 표준화 텍스트이며 그대로 운영 DB에 INSERT할 자료가 아니다.
- `runtime-smoke.json`: 정답지와 별도로 작성한 실제 SettingToolItem 형식의 합성 RDB 응답과 고정 모델 계획/선택. 최신값·후속 질문 2문항에만 사용한다.
- `app/chatbot/evaluation/contracts.py`: Dataset, Run, ReviewSet 계약.
- `scoring.py`: ID·접근 경계·내용 hash·인용 위치 검사와 검토 결과 집계. 자연어 문자열 포함 여부로 정답을 판정하지 않는다.
- `__main__.py`: 파일 전용 CLI. .env, DB, 모델/임베딩/judge client를 만들지 않는다.
- `tests/support/chat_evaluation_replay.py`: 기존 Python QA·HTTP 직렬화·SQLite outbox를 실행하는 2문항 연결 재현기. Spring과 모델 응답은 대역이며 소켓 연결을 차단한다.

Gold를 바꿔 관측한 답변에 맞추지 않는다. `export-inputs`는 질문·최근 대화·시나리오 snapshot만 내보내고 필수 사실·기대 outcome·금지 단정·검토 지침·고정 응답은 제외한다. 후속 대화 입력의 이전 답변은 의도적으로 틀릴 수 있다.

## 채점 방식

1. **실행/출처 검사:** 관측 근거가 해당 실행의 snapshot·작품·회차·검색 방식에 속하는지 확인한다. 파기/구 버전 원문, 미관측 ID, 변조된 인용 위치·내용은 실패다. 회차 제한으로 현재 설정을 과거 snapshot처럼 취급하지 않는다.
2. **의미 검토:** 별도 ReviewSet에 실제 답변의 정확한 구간, 사실 적합성, 인용의 의미적 지지, 충족한 필수 사실, 금지 단정 여부와 판정 이유를 기록한다. 검토자와 전체 주장 검토 여부도 명시한다.
3. **묶음 근거:** 필수 사실의 `support_sets`는 바깥 목록이 대안(OR), 내부 ID 목록이 함께 필요한 근거(AND)다. 이번 답변에 실제 연결한 유효 근거로 충족했는지 확인한다.
4. **무결성:** Dataset → Run → 개별 Observation → ReviewSet을 canonical JSON SHA-256으로 묶는다. 원시 trace 파일 hash와 trace의 `observations_sha256`도 검사해 정규화 답변만 사후 교체하는 것을 거절한다. 이 hash는 보존한 자료의 일치 검사이지 provider 실제 실행을 암호학적으로 인증하는 수단이 아니다.
5. **미검토:** 구조 검사만 통과한 응답은 PENDING이다. HUMAN/INTERNAL_MODEL 판정을 구분하며 LLM_JUDGE 단독 검토는 완료로 처리하지 않는다. 의미 검토의 전체 주장 식별·판정 정확성은 검토자의 책임이고 이 도구가 의미 함의를 자동 증명하지 않는다.

분자/분모를 함께 기록한다. 전체 문항의 실행 완료율·기대 outcome 일치율, 답변 가능한 문항의 응답률, 의미 검토율, **검토한** 주장의 정확도·근거 지지율, 전체 요구 중 지지가 확인된 필수 사실 비율을 분리한다. 미실행 문항을 분모에서 빼거나 미검토를 성공으로 세지 않는다. 모두 거절하면 주장 정확도는 분모 0의 null이며 답변 가능 문항 응답률은 0이 된다.

필수 사실 충족률은 확인된 사실/전체 요구다. 미실행·미검토도 아직 충족을 확인하지 못했으므로 분모에 남는다. 부분 실행에서 이 수치를 모델의 정답률로 보고하지 않는다.

## 세 방식의 비교 조건

- `RDB_DIRECT`: 현재값·이력·설정에 연결된 직접 근거.
- `VECTOR_ONLY`: 현재 유효하고 벡터가 준비된 원문 CHUNK.
- `HYBRID`: RDB/직접 근거와 유효 원문. 벡터 미준비 텍스트도 키워드 검색 대상이 될 수 있다.

같은 데이터 hash·코드 SHA·생성 모델·추론·프롬프트 묶음·색인/임베딩 버전·차원·캐시·동시성·실행 종류로 만든 결과만 비교한다. 방식별 조회 예산은 따로 기록한다. 이 도구는 비교용 결과 파일을 읽으며, 세 검색 엔진을 실제로 실행하거나 SQL/벡터 순위를 대신 계산하지 않는다.

각 질문의 필수 사실 전부에 해당 방식이 접근할 수 있으면 FULL, 일부 정보가 없으면 LIMITED로 나눈다. 이는 검색 성공 여부가 아니라 **접근 가능한 정보의 차이**다. 예: 원문에는 35만 있고 사용자가 현재값을 42로 고쳤다면 VECTOR_ONLY의 정보는 부족하다. LIMITED 문항을 숨기지 않고 전체·유형별·정보 가용성별 결과를 함께 남긴다. 특정 경로만 정답으로 강제하지 않고 최종 내용·근거·한계를 평가한다.

## 비용·지연

`AUTHORED_CONTROL`은 채점기 대조군, `SCRIPTED_REPLAY`는 고정 출력 연결 검증, `PROVIDER_CAPTURE`는 별도로 수집한 실제 응답이다. 세 종류를 섞어 비교하지 않는다.

고정 출력의 토큰/시간은 실제 금액·p50/p95로 환산하지 않는다. 실제 캡처도 전체 문항의 계측 완료, 모든 호출의 알려진 usage와 가격 기준이 있어야 전체 비용을 내며, 누락/UNKNOWN/미완료는 null이다. 확인된 비용 부분합은 전체 비용과 구분한다. 실패 호출도 포함하며 cached input은 input의 부분집합이다. 지연은 기록된 성공·실패의 표본 수와 nearest-rank p50/p95를 내고 20개 미만 표본의 한계를 표시한다.

실제 가격·provider 호출·query embedding·재시도·색인/judge 비용 분리 수집은 후속 adapter 작업이다. 초기 baseline 전 품질/비용/지연 임계값을 임의로 확정하지 않는다. `release_ready`는 이 단계에서 항상 false다.

## 실행

저장소 루트에서 실행한다. 출력 경로는 새 경로여야 하며 기존 자료를 덮어쓰지 않는다. 보고서는 기본 0600, 출력 디렉터리는 0700이다.

```sh
.venv/bin/python -m app.chatbot.evaluation validate \
  --dataset tests/fixtures/chat-evaluation-v1/dataset.json

.venv/bin/python -m app.chatbot.evaluation export-inputs \
  --dataset tests/fixtures/chat-evaluation-v1/dataset.json --output /tmp/chat-eval-inputs.json

.venv/bin/python -m app.chatbot.evaluation schemas --output-dir /tmp/chat-eval-schemas

.venv/bin/python -m tests.support.chat_evaluation_replay --output-dir /tmp/chat-eval-smoke

.venv/bin/python -m app.chatbot.evaluation score \
  --dataset tests/fixtures/chat-evaluation-v1/dataset.json \
  --run /tmp/chat-eval-smoke/run.json --trace /tmp/chat-eval-smoke/trace.json \
  --output-dir /tmp/chat-eval-score
```

위 score는 의미 검토가 없으므로 2문항 실행 완료, 나머지 24문항 미실행, 전체 26문항 PENDING이 정상이다. 검토한 ReviewSet 파일이 있으면 `--reviews <파일>`을 추가한다. 2문항을 전체 데이터의 정답률 100%로 환산하지 않는다.

```sh
.venv/bin/python -m app.chatbot.evaluation compare \
  --dataset tests/fixtures/chat-evaluation-v1/dataset.json \
  --run /tmp/rdb-run.json /tmp/vector-run.json /tmp/hybrid-run.json \
  --trace /tmp/rdb-trace.json /tmp/vector-trace.json /tmp/hybrid-trace.json \
  --reviews /tmp/rdb-review.json /tmp/vector-review.json /tmp/hybrid-review.json \
  --output-dir /tmp/chat-eval-comparison
```

compare의 파일들은 실제 각 방식 실행 후 따로 수집해야 한다. 예제의 경로가 준비된 baseline 파일이라는 뜻은 아니다. 입력 JSON schema는 `schemas` 명령으로 확인한다. trace JSON에는 정규화된 모든 Observation의 `model_dump(mode="json")` 목록을 canonical fingerprint한 `observations_sha256`을 저장한다. 예측을 정답지에서 생성·보충하는 adapter는 허용하지 않는다.

검증: `.venv/bin/pytest -q tests/test_chat_evaluation.py`. 회귀에는 날씨 근거/반지 주장, 45개 중 1개 이력, 전면 거절, 미검토·누락 실행, 타 작품/파기/구버전 근거, 사후 답변 교체, 중복 기록, UNKNOWN usage와 조건 불일치 비교가 포함된다.

[내부 검토 기록](evaluations/chat-evaluation-v1-internal-review.md) · [중앙 이슈 #242](https://github.com/catchhole-soma/catchhole-backend-java/issues/242) · [최적화 #247](https://github.com/catchhole-soma/catchhole-backend-java/issues/247)
