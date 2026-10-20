# 분석 Worker 실행과 Spring 호출 트레이스 (#271, 4~7단계)

Python Worker가 직접 OpenTelemetry Span을 만들고, 실행 중 Spring HTTP 요청에 W3C
`traceparent`를 전달한다. Spring의 Java Agent·Collector 연결은 Java 저장소의 로컬 구성으로
별도 검증한다. 7단계는 운영 환경 변수 연결과 배포 전 검사를 준비하며 실제 운영
Collector·Tempo 배포와 활성화가 완료됐다는 의미는 아니다.
기본 설정은 OFF이며 이 변경만 배포해도 운영 트레이스 전송이 시작되지는 않는다.

6단계의 합성 Worker·전송 장애 실험은 아래 재현 절차를 따른다. Head/Tail 실험과
전체 결과 해석은 Java 저장소의 `docs/tracing-validation.md`에 정리되어 있다.
운영 사설 OTLP 주소·기본 OFF·큐/종료 제한·활성화와 rollback 확인은
[Worker 운영 배포 문서](../deploy/WORKER_EC2_DEPLOYMENT.md#worker-트레이스-운영-적용-준비-271-7단계)를 따른다.

## 추적하는 실행 범위

CLI 프로세스에서 `TracerProvider`를 한 번 만들고 재사용한다. Worker가 Job을 claim한 뒤
`process_claimed`를 실행하는 시도마다 새로운 `analysis.job.execute` root Span을 만든다.
같은 Job을 재실행하면 Job ID는 같고 Trace ID는 새로 생긴다. 한 실행 안의 LLM 재시도는
같은 Trace 안에 별도 호출 Span으로 남는다.

root에는 Job ID·claim 시도·작업 종류·출처 종류·분석/검수 모드·재개 checkpoint와
claim payload에 이미 있는 원문 문자 수를 기록한다. 원문을 추가로 읽어서 계측하지 않는다.
초기 사용자 HTTP 요청, DB 큐 대기, Worker 실행 슬롯 대기, claim 자체는 이 root 범위에
포함하지 않는다. 나중에 별도로 claim되는 숨김 비교 Job도 독립된 실행이다.

```text
analysis.job.execute                      이번 claim 이후 실행
├─ analysis.prepare                       청크·입력 준비
├─ analysis.character.prepare             인물 후보 준비·저장, 회차 병렬 추출 포함
│  ├─ analysis.character.extract          한 청크의 인물 추출
│  └─ analysis.world.extract              같은 청크의 세계관 추출
├─ analysis.character.compare             실제 비교 또는 숨김 Job 인계
├─ analysis.world.prepare                 세계관 결과 정리·후보 게시
├─ analysis.world.compare                 세계관 비교
└─ analysis.finalize                      기존 완료 보고 구간
```

이 트리는 대표적인 회차 실행이다. 설정집·checkpoint 재개·재비교 Worker는 실제 실행한
경로만 기록한다. 세계관 추출은 결과를 재사용할 수 없으면 세계관 준비 단계 안에서 실행된다.
건너뛴 단계·숨김 Job 인계는 실제 추출/비교 성공으로 해석하지 않도록 구분한다.
선택적인 임베딩·검색 준비 실패가 기존 정책에 따라 허용되더라도 해당 하위 Span에서 실패를
확인할 수 있다. 그 실패를 새로 전체 분석 실패로 바꾸지는 않는다.

## 병렬 실행과 시간 해석

각 추출 코루틴 안에서 Span을 시작한다. `asyncio.gather`가 Task를 만들 때 공통 부모의
Context를 각각 상속하므로 인물·세계관 추출은 같은 부모 ID를 가진 형제 Span이 된다.
각 Task에서 활성 Span을 바꿔도 다른 Task의 활성 Span은 바뀌지 않는다.
동기 DB/S3 작업을 bounded executor에 제출할 때는 매번 `copy_context()`로 문맥을 전달한다.

형제 관계 자체가 동시 실행을 뜻하지는 않는다. 시간 구간의 겹침도 함께 확인한다.
동시에 시작한 3초·8초 추출을 기다리는 시간은 다른 작업이 없으면 약 8초이며 합계 11초가
아니다. 실제 상위 단계 시간은 부모 Span에서 읽는다. 청크 사이의 기존 순서는 유지한다.

## LLM 호출과 주변 대기

| Span | 측정하는 구간 |
| --- | --- |
| `llm.request` | 입력 토큰 사전 계산, 슬롯 대기, 예약, 호출, 정산, transport 재시도까지 텍스트 요청 전체 |
| `llm.semaphore.wait` | 동시 호출 슬롯을 획득할 때까지의 대기 |
| `llm.tokens.reserve` | 실제 토큰 예약 API 작업 |
| `llm.call` | 기존 delegate의 텍스트 생성 호출 한 번 |
| `llm.tokens.finalize` | 실제 사용량 정산 API 작업 |
| `llm.tokens.release` | 사용량 미확인·취소 등에 따른 예약 해제 API 작업 |
| `llm.retry.backoff` | 기존 transport 재시도 정책의 sleep |

`llm.call` 하나가 2초여도 `llm.request`는 슬롯 대기나 정산 때문에 12초일 수 있다.
상위 request에서 각 하위 구간을 확인한다. 상위 schema/출력 절단 복구는 여러 request를
만들 수 있으므로 transport 재시도 번호와 혼동하지 않는다. 기존 Prometheus LLM Histogram은
계속 delegate 한 호출만 재며 예약·정산·대기를 합산하지 않는다.

정산 Span은 `shield`를 기다리는 분석 Task 바깥이 아니라 실제 원장 API를 수행하는
별도 코루틴 안에서 연다. 분석 Task가 취소돼도 기존 정책으로 정산이 계속되면 실제 완료까지
기록한다. 따라서 정산 자식 Span은 분석 부모 Span보다 늦게 끝날 수 있다.
이 계측은 프로세스 강제 종료 뒤에도 정산이나 Span 전송이 보장된다는 의미가 아니다.

## Spring HTTP 전파 (5단계)

`SpringWorkerClient._request`에서 실제 `httpx.AsyncClient.request` 한 번마다
`spring.http.request` CLIENT Span을 만든다. 이 client는 최초 claim에도 쓰이므로 전역
HTTPX 자동 계측 대신 공통 요청 경계에서 명시적으로 계측한다. HTTPX 자동 계측을 중복으로
활성화하지 않는다. 자동 계측이 불가능해서가 아니라 기록 범위와 개인정보 제한을 맞추기 위한
선택이며 새로운 instrumentation 패키지는 필요하지 않다.

```text
analysis.job.execute                   Worker 실행 root
└─ llm.tokens.finalize                 실제 정산 작업
   └─ spring.http.request [CLIENT]     Worker가 Spring HTTP 응답을 기다리는 구간
      └─ Spring SERVER Span           Java Agent를 켰을 때 동일 Trace로 연결
         └─ JDBC Span                 Spring 내부의 지원되는 JDBC 호출
```

- root 진입 시 `ContextVar`에 해당 runtime의 실행 문맥을 설정한다. 상속한 Task·heartbeat·
  shield 원장 작업에서도 유효하며 부모 root가 먼저 끝나도 실제 정리 작업은 이어질 수 있다.
  다른 독립 Task와 root 종료 이후의 호출에는 전파하지 않는다.
- 최초 `claim()`은 root 안에서 잘못 호출해도 `trace_request=False`로 명시적으로 제외한다.
  실행 중 `claim-next`로 비교 후보를 받는 요청은 해당 Job 실행의 하위 호출로 기록한다.
- 매 요청의 헤더 복사본에 `TraceContextTextMapPropagator`로 W3C 문맥만 주입한다. 공유
  `AsyncClient.headers`는 바꾸지 않으며 기존 내부 인증·lease 헤더는 유지한다. baggage를
  새로 주입하지 않는다. 전파 ID는 권한이나 인증 수단이 아니다.
- HTTP CLIENT가 생성된 뒤 주입하므로 `traceparent`의 parent-id는 CLIENT Span ID다.
  Spring이 이 헤더를 읽으면 SERVER Span을 같은 Trace의 자식으로 만든다. Java Agent를
  끄면 Python CLIENT Span만 보이며 헤더만으로 Spring Span이 자동 생성되지는 않는다.
- 현재 Span이 기록 중인지가 아니라 Worker 실행 문맥과 유효한 SpanContext를 검사한다.
  이후 head sampling으로 선택하지 않은 Trace도 sampled 비트 0을 전달할 수 있다.
  SDK 1.45.1은 random Trace ID 비트도 사용할 수 있으므로 flags를 `01`/`00` 문자열로
  고정 해석하지 않고 최하위 sampled 비트를 읽는다.
- Span에는 제한된 `spring.operation`, HTTP method·status와 고정 오류 분류만 남긴다.
  URL의 UUID는 고정 route map에 대응하는 데만 사용하고 원본 URL·query·body·header·
  예외 메시지는 기록하지 않는다. 등록되지 않은 경로·method는 `other`다.
- HTTP 4xx/5xx는 CLIENT 오류로 표시하되 기존 업무 예외 변환·원장 재시도 정책을 유지한다.
  timeout/network/protocol 오류도 기존 `SpringWorkerTransportError`의 원인을 보존한다.
  취소는 그대로 전파하고 HTTP 재전송을 계측 코드에서 추가하지 않는다.
- 헤더 주입이 실패하면 원래 헤더로 요청을 한 번 수행한다. Span 생성·속성·종료 실패도
  업무 요청을 다시 실행하지 않는다. 이 경우 Trace 일부나 서버 연결이 빠질 수 있다.

CLIENT 시간에는 연결 풀 대기·네트워크 왕복·Spring 처리·응답 읽기가 포함될 수 있다.
Spring SERVER/JDBC와 중첩되므로 각각의 시간을 단순 합산하지 않는다. 최초 사용자 요청·
DB 큐 대기·초기 claim까지 하나의 Trace로 이어졌다는 의미도 아니다. Python이 claim에
헤더를 보내지 않더라도 Java의 root sampling 설정에 따라 독립 서버 Trace가 생길 수 있다.

## 실행 결과와 데이터 범위

5단계의 Java Agent·실제 HTTP/JDBC·Tempo 연결 검증은 Java 저장소의
`scripts/tracing/verify_worker_spring.py`로 수행했다. 실제 Python client와 Java token
Controller를 합성 H2 서비스에 연결해 동시 실행 두 건·HTTP 500 한 건의 3개 Trace,
총 21개 Span을 전송하고 Tempo에서 부모 관계를 다시 확인했다. 부모 없는 요청과
sampled=0 전파 제외, 요청 동시성, 전송 전 원문 속성 제한도 검사했다. 실제 원장 정책이나
운영 PostgreSQL·분석 품질 검증은 아니다. 재현 절차·Trace ID는 Java의
`docs/local-tracing.md`에 기록되어 있다.

- root의 `outcome=success`는 `process_claimed`가 정상 반환했다는 뜻이다.
  사용자에게 최종 결과가 모두 준비됐거나 모든 비교가 성공했다는 뜻은 아니다.
- 일부 실패를 허용하는 비교 경로는 비교 Span에 완료·실패 개수와 부분 실패 상태를 남긴다.
  설정 충돌 발견, `REVIEW_REQUIRED`, `EXCLUDE`는 기술적인 비교 실패가 아니다.
  비교 실패 개수만으로 주체 해소를 포함한 전체 분석 품질을 판정하지 않는다.
- 원고·prompt·모델 응답·제목·인물명·S3 key·lease token·인증 정보·예외 원문은 기록하지 않는다.
  기본 exception event와 예외 메시지 기반 status description도 비활성화한다.
- Job ID는 실행을 찾기 위한 Span 속성이다. Prometheus label에는 추가하지 않는다.
  LLM 목적·모델·오류 분류와 결과는 제한된 값으로 기록한다.
- 계측 초기화·속성 기록·Span 종료·전송 오류가 기존 분석 결과, 재시도, 슬롯 반환,
  원장 정산을 바꾸지 않도록 처리한다. 메모리 큐가 가득 차거나 전송이 실패하면 트레이스는
  유실될 수 있다. 토큰 과금의 기준은 계속 Spring 원장이다.

## 설정과 전송 정책

| 항목 | 값 |
| --- | --- |
| `AI_WORKER_TRACING_ENABLED` | 기본 `false` |
| `OTEL_SERVICE_NAME` | 기본 `catchhole-ai-worker` |
| `OTEL_EXPORTER_OTLP_TRACES_ENDPOINT` | 로컬 앱 기본 `http://localhost:4318/v1/traces`; 운영 Compose 기본 빈 값, 활성화 시 사설 주소 필수 |
| 환경 | 기존 `APP_ENV`를 Resource 환경 값으로 사용 |
| SDK / OTLP HTTP exporter | `1.45.1` |
| 샘플링 | `ParentBased(ALWAYS_ON)`; 새 root 선택, 자식은 부모 결정 상속 |
| SDK 주기 / 최대 배치 / 큐 | 1초 / 256 Span / 1,024 Span |
| exporter HTTP timeout | 5초 |

Mac에서 SDK를 실행할 때의 주소가 `localhost:4318`이다. 컨테이너의 localhost는 그
컨테이너 자신이므로 운영 Worker에서는 7단계 배포 문서의 사설 Collector 주소를 지정한다.
종류별 flag가 공통 flag보다 우선하며, 최초에는 기존 캐릭터 비교 한 프로세스만 켜서 확인한다.
SDK와 Collector의 batch는 서로 다른 큐다. all-on은 이번 연결 검증 정책이며 운영 샘플링
확정이나 모든 Span의 영구 저장 보장이 아니다. Tail Sampling은 아직 적용하지 않는다.

종료 시 기존 Worker drain·자원 정리 이후 SDK flush/shutdown을 별도 스레드에서 수행한다.
flush timeout이나 HTTP timeout은 프로세스 전체의 종료 시간을 제한하는 값이 아니다.

## 격리된 검증

저장소 전용 Python 환경에서 실행한다. 실제 `.env`, Worker 프로세스, DB, S3, 유료 AI API를
사용하지 않고 InMemory exporter와 fake/mocks로 계측 경계를 검사한다.

```bash
python -m pip install -e ".[dev]"
python -m pytest tests/test_worker_tracing.py tests/test_llm_tracing.py tests/test_worker_pipeline_tracing.py tests/test_spring_http_tracing.py
python -m pytest
```

검증 결과와 미검증 범위는 아래 기록에 추가한다. 자동 테스트 통과를 실제 모델 품질,
운영 부하, HTTP/JDBC 분산 추적 성공으로 해석하지 않는다.

### 2026-10-20 로컬 검증 결과

- Python 3.12 전용 `.venv`에서 editable install과 `pip check` 통과.
- 전체 pytest: **4,149 passed, 134 skipped, 1 warning**, 17.09초.
  경고는 Starlette TestClient의 httpx 관련 deprecation이다. 별도 환경을 요구하여 생략된
  테스트를 통과한 것으로 집계하지 않는다.
- 새 tracing 테스트 47개: 독립 root/동시 Context, 실제 병렬 추출의 형제 관계·시간 겹침·
  청크 순서, checkpoint/인계, 정상 반환과 부분 비교 실패, retry/슬롯/원장 수명,
  실제 메트릭 시간 경계, 민감값 차단, 계측 실패 및 초기화 중 종료 요청을 검사했다.
- 변경 Python 파일 Ruff, `git diff --check` 통과. 설정 모델 override는 메트릭 OFF에서도
  tracing 자체의 허용 목록으로 기록한다. 초기화 중 KeyboardInterrupt/SystemExit는 부분
  생성 자원을 정리하고 프로세스 상태를 복원한 뒤 원래 종료 요청을 전달한다.
- 기존 로컬 Collector→Tempo에 `catchhole-ai-worker-stage4-check` 서비스로 합성 Trace를
  전송하고 Grafana datasource proxy API에서 다시 읽었다. 원고·DB·S3·Spring·LLM 네트워크
  호출은 fake이며, 실제 Worker/runner 및 metering 코드를 사용했다.

| 합성 시나리오 | Trace ID | 확인한 Span |
| --- | --- | --- |
| 실제 Worker의 두 청크 병렬 추출 | `d2b3951a06087d138910bd8317745689` | 11개 |
| 실제 metering의 429→재시도 성공 | `6f61e4a459d2a2efe182b37d298b3120` | 11개 |

두 Trace 모두 로컬 SDK가 만든 Span ID·이름·부모 ID와 Tempo 응답을 대조했다.
두 개의 독립 시나리오이며 하나의 완성된 운영 분석 Trace로 합쳐 검증한 것은 아니다.
브라우저 로그인 후 화면 확인과 운영 데이터 검증은 수행하지 않았다.
위 ID는 기존 Tempo의 24시간 블록 보존 정책에 따라 이후 조회되지 않을 수 있다.

로컬 합성 검증 스크립트·응답·결과는 저장소 밖
`/private/tmp/catchhole-gh271-stage4-verification`에 보관했다. 임시 파일이며 PR에는 포함하지
않는다. 이 실행에서 만들어진 시간은 실제 AI 응답 지연이나 서비스 처리 성능 측정값이 아니다.

### 2026-10-20 5단계 Python 검증

- 신규 `test_spring_http_tracing.py` 36개: 동시 Job의 Trace/CLIENT 부모 관계, 요청별
  인증·lease 보존, 최초 claim 제외와 작업 내부 claim 포함, OFF/실행 밖 호출 제외,
  sampled=0 전파, 부모 종료 뒤 정산 Task 문맥, HTTP·transport·취소, 계측 고장 시
  원래 요청 한 번 유지, 원장 재시도, 민감값 차단을 MockTransport로 검사했다.
- 기존 Spring client·Worker tracing·LLM tracing을 포함한 집중 테스트: **100 passed**.
- 최종 전체 재검사: **4,185 passed, 134 skipped, 1 warning**, 17.15초.
  경고는 기존 Starlette TestClient/httpx deprecation이다. 변경 Python 파일 Ruff와
  `git diff --check`도 통과했다.
- 이 Python 단위 테스트는 실제 Spring/JDBC 연결을 수행하지 않는다. 분산 연결 검증 결과는
  Java 저장소 `docs/local-tracing.md`에 별도로 기록한다. 운영 배포·운영 sampling은 미수행이다.

## 6단계: 합성 실행과 전송 실패 재현

실제 runner·AnalysisJobWorker·MeteredTextGenerationClient를 사용하고, 입력·저장소·Spring·
LLM 응답만 합성 fixture로 대체한다. 정상, 느린 호출, 호출 슬롯 대기, 429 재시도,
Task 취소 후 정산, 동시 두 Job의 여섯 상황을 실행한다. 기본은 메모리 검증이며
`--export-otlp`를 명시한 경우에만 localhost Collector로 보낸다.

```bash
TRACE_LAB_OUTPUT=$(mktemp -d /tmp/catchhole-tracing-stage6.XXXXXX)
.venv/bin/python -m scripts.tracing.verify_worker_scenarios \
  --export-otlp --output "$TRACE_LAB_OUTPUT/worker.json"
.venv/bin/python scripts/tracing/verify_exporter_outage.py \
  --output "$TRACE_LAB_OUTPUT/outage.json"
```

- `scripts/tracing/_worker_scenario_fixtures.py`: 합성 응답·빈 추출 결과·지연·원장 동작을 준비한다.
  실제 외부 모델·Spring·DB·S3에 접속하지 않으며 토큰 사전 계산은 고정 합성 값으로 대체한다.
- `scripts/tracing/verify_worker_scenarios.py`: 부모 관계·Trace 분리·시각·오류/취소 상태,
  기존 LLM Histogram의 delegate 범위·추가 호출 Counter·active Gauge 복원을 검증한다.
  출력에는 Tempo에서 다시 대조할 Span ID·부모·시각과 합성 메트릭을 저장한다.
- `scripts/tracing/verify_exporter_outage.py`: 임시 loopback 수신기의 연결 거절·503·지연,
  작은 큐 초과를 재현한다. 업무 완료·실패한 전송·복구 뒤 새 전송·전송 전 drop을 구분한다.
  이 스크립트는 전체 Worker 파이프라인·Prometheus 메트릭·Tempo 저장을 검사하지 않는다.

OTLP 실험은 환경의 OTEL 인증 헤더와 HTTP proxy를 격리한다. 출력 파일은 새 파일로 만들고,
공유 Collector를 중단하거나 운영 플래그·동시성·재시도 정책을 바꾸지 않는다.
축소한 retry 시간·queue·export timeout과 합성 LLM 지연은 실험 조건이며 운영 권장값이 아니다.
분석 Task 취소와 프로세스 강제 종료·작업 회수는 다른 상황이다.

현재 Python `ParentBased(ALWAYS_ON)`과 Java 부모 결정 상속·Collector Tail OFF를 유지한다.
실제 factory가 sampler를 지정하므로 `OTEL_TRACES_SAMPLER` 환경변수만으로 변경하지 않는다.
운영 활성화·자원·보관 설정은 7단계에서 검토한다.

### 2026-10-20 6단계 확인 결과

- 여섯 합성 시나리오에서 **7개 Trace·99개 Span**을 생성해 로컬 Collector로 전송했다.
  Java 저장소의 `scripts/tracing/verify_worker_trace_readback.py`로 Tempo에서 다시 읽고
  모든 Span의 ID·이름·부모 ID·시작/종료 시각이 일치함을 확인했다.
- 호출 슬롯 대기 151.241ms와 실제 LLM 호출 31.180ms가 분리됐고, 기존 LLM Histogram은
  delegate 호출 31.126ms만 기록했다. 429 시나리오는 호출 2회·추가 재시도 1회와
  backoff 151.129ms를 구분했다.
- 분석 root는 53.380ms 후 취소됐지만, 이미 시작한 shield 정산은 총 173.018ms 동안
  실행된 뒤 성공했다. 두 Job의 동시 실행에서도 각 Trace의 부모 관계가 섞이지 않았다.
- 연결 거절·503·느린 수신 응답에서도 합성 업무는 완료됐다. queue 4/batch 1 실험에서는
  종료 Span 21개 중 5개만 수신돼 16개가 전송 전에 탈락했다. 복구 후 새 Trace는
  전송됐지만 실패한 과거 Span이 복원되지는 않았다.
- `tests/test_worker_scenario_verification.py`: **11 passed**. 외부 I/O 차단, 실행별
  Span 경계, 기존 메트릭 범위, 환경·인증 격리와 결과 파일 보호를 확인했다.

모든 시간은 합성 지연 실험값이다. 실제 LLM 성능·프로세스 강제 종료 복구·운영 배포를
검증하지 않았다. 샘플링과 전체 결과 표는 Java 저장소 `docs/tracing-validation.md`에 있다.

### 2026-10-21 운영 준비 변경 최종 검증

- `origin/main`의 `6ec1bd5`를 다시 확인했으며, 기반 이후 새 커밋이나 병합 충돌은 없었다.
- Python 3.12에서 `.venv/bin/python -m pytest -q`: **4,227 passed, 134 skipped,
  1 warning**, 21.56초. 생략된 환경 의존 테스트는 통과로 집계하지 않으며 경고는 기존
  Starlette TestClient/httpx deprecation이다.
- 변경 Python 파일 Ruff, `pip check`, `git diff --check` 통과. 기존 배포 도구의
  `EXE001`, `PLW1510`, `TRY004`, `BLE001` 네 규칙 위반은 이번 변경 이전에도 존재하며,
  해당 파일에만 그 네 규칙을 제외하고 검사했다.
- 운영 설정 집중 검증은 **153 passed, 5 skipped**였고, 생략된 opt-in Compose/bootstrap
  다섯 사례를 별도로 활성화해 **5 passed**를 확인했다. Compose 검사는 설정 렌더링만
  수행하며 운영 컨테이너를 시작하거나 유료 LLM·DB에 접속하지 않는다.
- 기본 OFF, 캐릭터 비교 한 프로세스 canary, 종류별 override와 사설 endpoint 검증을
  확인했다. 이 기록은 실제 운영 배포·운영 Trace 조회 완료를 의미하지 않는다.
