# AI 토큰 예약과 Prompt Cache 검증

AI Worker의 토큰 예약 계산과 OpenAI Prompt Cache가 실제 분석 호출에서 어떻게 동작하는지 확인한 기록입니다.

이 문서는 특정 로컬 DB 행을 제품 정책으로 고정하기 위한 문서가 아닙니다. 같은 조건으로 다시 검증할 때 비교 기준으로 사용합니다.

## 검증 환경

- 검증일: 2026-08-03
- 당시 모델: `gpt-4.1-mini` (GPT-5.6 Terra 전환 전 측정)
- API: OpenAI Responses API
- 대상: 같은 작품에서 연속 실행한 분석 Job 3개
- 원장 범위: 로컬 `ai_token_usages`를 `created_at`, `request_id` 순으로 정렬한 51~61번째 행
- 호출 구성: 설정 추출 9회, 주체 해소 2회

DB의 행에는 고정된 순서가 없으므로 재검증할 때는 화면에 보이는 행 번호 대신 분석 시작 시각이나 `analysis_job_id`로 범위를 선택합니다.

## 검증 결과

### Prompt Cache

| 목적 | 호출 | 캐시 적중 | 호출 적중률 | 입력 토큰 | 캐시 입력 토큰 | 토큰 캐시 비율 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `SETTING_EXTRACTION` | 9 | 8 | 88.9% | 52,734 | 31,744 | 60.2% |
| `SUBJECT_RESOLUTION` | 2 | 0 | 0% | 9,277 | 0 | 0% |

설정 추출의 첫 요청만 cache miss였고, 이후 8회는 요청마다 3,968 input token이 cache hit로 기록되었습니다. 동일한 정적 prefix가 먼저 cache에 쓰이고 뒤이은 요청에서 재사용되는 기대 흐름과 일치합니다.

`input_tokens`는 캐시 적용 후 줄어드는 값이 아닙니다. `cached_input_tokens`는 전체 `input_tokens`에 포함되는 부분집합이며, 비용을 계산할 때만 일반 input 단가와 cached input 단가를 나누어 적용합니다.

예를 들어 다음 usage가 반환되었다면:

```text
input_tokens        = 6,217
cached_input_tokens = 3,968
output_tokens       = 9
```

새로 처리한 input은 `6,217 - 3,968 = 2,249` token이지만 원장에는 전체 prompt 길이인 6,217 input token을 그대로 저장합니다.

### 토큰 예약량

| 목적 | 재시작 전 평균 예약 | 재시작 후 평균 예약 | 재시작 후 평균 실제 사용 | 예약/실사용 배율 |
| --- | ---: | ---: | ---: | ---: |
| `SETTING_EXTRACTION` | 26,409.8 | 10,689.7 | 6,185.8 | 1.73배 |
| `SUBJECT_RESOLUTION` | 19,094.0 | 6,347.0 | 4,711.5 | 1.36배 |

Worker 재시작 전 프로세스는 변경 전 byte 기반 예약 코드를 계속 사용했습니다. 재시작 후에는 모델 tokenizer로 계산한 input에 10%와 256 token의 안전 여유, 호출별 최대 output을 더하는 당시 정책이 적용되었습니다.

이 검증 당시 설정 추출 상한은 `max_output_tokens=4000`이었습니다. 현재 캐릭터 추출 기본값은 6,000이고 절단 시 12,000으로 한 번 확장하므로 이 표의 예약량 수치를 현재 운영값으로 해석하지 않습니다. `reserved_tokens`는 provider 청구량이 아니라 호출 중 quota를 임시로 확보하는 값이며, 응답 후 실제 usage로 정산됩니다.

### 비용 추정

검증 당시 `gpt-4.1-mini` 표준 단가를 적용하면 51~61행의 추정값은 다음과 같습니다.

```text
cache가 없다고 가정한 비용: 약 $0.0297388
cache를 반영한 추정 비용:  약 $0.0202156
절감률:                     약 32.0%
```

가격은 변경될 수 있으므로 제품 코드와 원장에 위 단가를 상수로 저장하지 않습니다. 비용을 다시 계산할 때는 [OpenAI API Pricing](https://developers.openai.com/api/docs/pricing)의 현재 단가를 사용합니다.

## 설정 추출은 캐시되고 주체 해소는 캐시되지 않는 이유

### 설정 추출

설정 추출은 캐시하기 좋은 구조입니다.

1. 약 2,620 token의 고정 시스템 프롬프트가 요청 앞부분에 있습니다.
2. 활성 설정 schema를 `schemaKey`와 JSON key 기준으로 정렬해 같은 schema 집합이 항상 같은 문자열이 되도록 직렬화합니다.
3. 시스템 지침과 schema를 먼저 배치하고, 회차 metadata와 chunk 원문을 뒤에 둡니다.
4. 같은 schema JSON에서 만든 fingerprint를 포함한 `prompt_cache_key`를 재사용합니다.

따라서 같은 작품의 연속 요청은 캐시 최소 prefix 길이를 넘는 동일한 앞부분을 공유합니다.

### 주체 해소

주체 해소의 고정 시스템 프롬프트는 약 638 token입니다. 그 뒤 user prompt에는 다음처럼 요청마다 달라질 수 있는 정보가 이어집니다.

- 현재 작품의 캐릭터 목록
- 이전·현재·다음 chunk 문맥
- 해소 대상 후보와 원문 인용

공통 prefix가 cache 가능 최소 길이에 도달하기 전에 동적 내용이 시작될 수 있으므로 `prompt_cache_key`가 같아도 exact prefix cache hit가 발생하기 어렵습니다. 캐시 적중을 위해 의미 없는 padding을 추가하면 input 자체가 늘어나므로 현재는 최적화 대상으로 삼지 않습니다.

위 수치는 `gpt-4.1-mini`로 측정한 역사적 결과입니다. 현재 운영 기본값은 `gpt-5.6-terra`, `reasoning.effort=none`이며 코드는 안정적인 `prompt_cache_key`만 전달합니다.

GPT-5.6의 implicit caching은 가장 최근 user/tool 경계만 기본 breakpoint로 사용하므로, 같은 key만으로 cache hit를 강제할 수 없습니다. 현행 prompt는 정적 시스템 지침과 schema를 앞에, 동적 회차·청크를 뒤에 배치해 implicit cache가 재사용되기 쉬운 구조를 유지합니다. explicit `prompt_cache_breakpoint`와 `prompt_cache_options.mode=explicit`은 아직 적용하지 않았으며, 도입할 경우 cache write 비용과 실제 적중률을 별도로 재검증합니다. 최소 cache 가능 prefix와 최신 동작은 [OpenAI Prompt Caching](https://developers.openai.com/api/docs/guides/prompt-caching)을 기준으로 확인합니다.

## 재검증 절차

1. AI Worker 코드를 변경했다면 실행 중인 Worker를 종료하고 다시 시작합니다.
2. 같은 작품의 여러 회차를 연속 분석해 같은 schema prefix가 재사용되게 합니다.
3. `ai_token_usages`를 분석 시작 시각 또는 `analysis_job_id`로 조회합니다.
4. 목적별로 다음 값을 비교합니다.
   - 호출 수
   - `cached_input_tokens > 0`인 호출 수
   - `sum(cached_input_tokens) / sum(input_tokens)`
   - 평균 `reserved_tokens`
   - 평균 `reserved_tokens / (input_tokens + output_tokens)`
5. 첫 설정 추출 요청은 cache miss이고, 같은 prefix의 후속 요청에서 cache hit가 생기는지 확인합니다.
6. Worker 재시작 후에도 예약량이 이전 수준이면 tokenizer 초기화 실패와 byte fallback 여부를 확인합니다.
7. 모델을 바꿨다면 과거 행과 섞지 말고 `model_name`으로 범위를 나누어 cached input 비율과 예약/실사용 배율을 다시 측정합니다.

## 판단 기준

- 설정 추출 첫 호출의 cache miss는 정상입니다.
- 같은 prefix의 후속 호출이 모두 hit한다는 보장은 없으므로 한두 건보다 연속 호출 묶음의 비율을 봅니다.
- 주체 해소 cache miss는 현재 prompt 구조상 정상이며 분석 실패가 아닙니다.
- `cached_input_tokens`는 `input_tokens`에 더하지 않습니다.
- 예약량은 실제량보다 커야 하지만, 재시작 후 설정 추출이 과거처럼 4배 이상이면 최신 예약 코드 적용 여부를 먼저 확인합니다.
- prompt 본문과 provider raw response는 원장에 저장하지 않고 token usage만 저장합니다.
- 이 문서의 비용 수치는 `gpt-4.1-mini` 측정 당시 참고값이며 GPT-5.6 Terra의 운영 비용으로 재사용하지 않습니다.
