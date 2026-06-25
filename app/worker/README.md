# worker

분석 작업을 실행하는 Worker 흐름을 두는 패키지입니다.

Spring 기준으로는 비동기 작업 executor 또는 batch worker에 가깝습니다.

## 역할

- Spring 내부 Worker API를 통해 실행할 분석 작업을 claim합니다.
- claim된 작업 payload를 기준으로 청킹, 설정 추출, 저장 같은 분석 단계를 실행합니다.
- 진행, 완료, 실패 상태는 DB를 직접 수정하지 않고 Spring 내부 API로 보고합니다.

다음 책임은 Worker에 넣지 않습니다.

- 사용자-facing API 응답 구성
- Spring `analysis_jobs` row 직접 상태 변경
- SQLAlchemy query 세부 작성
- LLM prompt 본문 작성

## 분석 상태와 처리 단계

분석 작업에는 `status`와 `current_step` 두 종류의 상태성 값이 있습니다.

### status

`status`는 분석 작업의 큰 생명주기를 나타냅니다.

예시:

- `PENDING`
- `RUNNING`
- `SUCCEEDED`
- `FAILED`
- `CANCELED`

현재 방향에서는 `status` 변경 책임을 Spring 서버가 가집니다.
Python Worker는 `analysis_jobs` 테이블을 직접 수정하지 않고, Spring 내부 API에 claim, complete, fail을 호출해 상태 변경을 요청합니다.

### current_step

`current_step`은 `RUNNING` 상태 안에서 현재 어떤 세부 단계를 처리 중인지 나타냅니다.

예시:

- `CHUNKING`
- `SETTING_EXTRACTION`
- `VALIDATION`
- `PERSISTING`

`current_step`은 사용자에게 진행 상황을 보여주거나, 실패 시 어느 단계에서 멈췄는지 파악하기 위한 값입니다.

## 현재 Spring API 동작

현재 Spring claim API는 요청 body의 `currentStep`을 받을 수 있습니다.

```text
POST /api/internal/v1/analysis-jobs/claim
```

claim 요청의 `currentStep`은 claim 성공 직후 `AnalysisJob.start(modelName, currentStep)`에 전달됩니다.
따라서 Python Worker가 claim 요청에 `currentStep`을 보내면 Spring은 해당 값을 작업의 시작 단계로 저장합니다.

또한 Spring에는 실행 중 단계를 갱신하는 별도 progress API가 있습니다.

```text
PATCH /api/internal/v1/analysis-jobs/{analysisJobId}/progress
```

따라서 현재 구조에서는 두 지점 모두 `current_step`을 바꿀 수 있습니다.

- claim 시점: 작업을 `RUNNING`으로 만들면서 초기 단계 기록
- progress 시점: 실행 중 세부 단계 변경

## 합의가 필요한 부분

`status`와 `current_step`은 서로 역할이 다르지만, `current_step`을 언제 바꿀지는 팀 합의가 필요합니다.

논의할 선택지는 다음과 같습니다.

### 선택지 1. claim에서 초기 current_step을 함께 기록

흐름:

```text
claim(currentStep=SETTING_EXTRACTION)
-> status = RUNNING
-> current_step = SETTING_EXTRACTION
```

장점:

- claim 직후부터 작업이 어느 단계로 시작됐는지 알 수 있습니다.
- Worker가 progress API를 호출하기 전에도 화면에 시작 단계가 보입니다.

주의점:

- claim 직후 다시 progress를 호출하면 같은 단계가 중복 기록될 수 있습니다.
- claim의 책임이 "작업 획득"과 "초기 단계 기록"으로 조금 넓어집니다.

### 선택지 2. claim은 status만 바꾸고 current_step은 progress에서만 기록

흐름:

```text
claim()
-> status = RUNNING
progress(currentStep=SETTING_EXTRACTION)
-> current_step = SETTING_EXTRACTION
```

장점:

- claim과 progress의 책임이 명확하게 분리됩니다.
- `current_step` 변경 지점이 progress API 하나로 모입니다.

주의점:

- claim 직후 progress 호출 전까지는 `current_step`이 비어 있을 수 있습니다.
- Spring claim DTO의 `currentStep` 필드를 사용하지 않게 됩니다.

## 현재 Python 구현 기준

현재 Python Worker는 Spring API 스펙에 맞춰 `claim` 요청에 `currentStep`을 보낼 수 있는 구조입니다.
다만 `current_step` 변경 정책이 확정되기 전까지는 다음 기준으로 코드를 읽습니다.

- `status`는 Spring이 관리합니다.
- Python은 DB row를 직접 변경하지 않습니다.
- Python은 Spring 내부 API에 진행, 완료, 실패를 보고합니다.
- `current_step`을 claim에서 보낼지, progress에서만 보낼지는 후속 협의 대상입니다.

