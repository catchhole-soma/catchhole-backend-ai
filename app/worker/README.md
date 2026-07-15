# worker

분석 작업을 실행하는 Worker 흐름을 두는 패키지입니다.

Spring 기준으로는 비동기 작업 executor 또는 batch worker에 가깝습니다.

여러 패키지를 가로지르는 전체 흐름도는 [AI Worker Workflow](../../docs/ai-worker-workflow.md)를 기준으로 확인합니다.
이 문서는 Worker 패키지의 책임과 상태/단계 정책을 중심으로 설명합니다.

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

## 현재 연결된 실행 흐름

`AnalysisJobWorker.run_once()`는 다음 순서로 한 개의 분석 작업을 처리합니다.

```text
Spring claim
-> progress 보고
-> episode별 S3 원문 청킹
-> episode별 저장 청크 batch 임베딩 및 DB 반영
-> chunk별 캐릭터 설정 후보 추출
-> evidence quote 위치 보정
-> 지칭어/placeholder 후보 subject fallback
-> setting_candidates 교체 저장
-> summaryJson 생성
-> Spring complete 보고
```

세부 책임은 다음 파일로 나뉩니다.

- `analysis_job_worker.py`
  - claim된 payload의 episode 목록을 순회합니다.
  - episode별 청킹·임베딩 서비스와 chunk별 설정 추출기를 호출합니다.
  - 생성된 episode/chunk/embedding/candidate 개수를 `summaryJson`으로 모아 Spring에 완료 보고합니다.
- `EpisodeS3ChunkingService`
  - episode_id로 DB의 episode를 조회합니다.
  - episode의 `content_s3_key`로 S3 원문을 읽습니다.
  - 읽은 원문을 `EpisodeChunkService`에 넘겨 기존 chunk 삭제 후 새 chunk 저장을 수행합니다.
- `CharacterSettingExtractor`
  - 저장된 chunk 하나를 LLM에 보내 캐릭터 설정 후보를 추출합니다.
  - LLM 응답 JSON을 `app/analysis/schemas.py` 기준으로 검증합니다.
- `ChunkEmbeddingService`
  - episode의 저장된 청크 텍스트를 한 번에 임베딩합니다.
  - 벡터와 모델·버전·생성 시각을 `episode_chunks`에 반영합니다.
  - 현재는 임베딩 단계의 예외를 Worker가 기록하고 설정 후보 추출을 계속하므로, commit되지 않은 임베딩은 후속 backfill 대상이 됩니다.
  - 외부 API 실패와 청크 누락 같은 데이터 정합성 실패를 구분하는 정책은 후속 작업으로 남아 있습니다.
- `evidence_span_resolver.py`
  - LLM이 반환한 `evidence_spans[].quote`를 chunk 원문에서 다시 찾습니다.
  - quote 위치를 `episode_chunks.start_offset`과 더해 회차 전체 기준 offset으로 보정합니다.
  - quote를 찾지 못하면 잘못된 위치를 저장하지 않도록 offset을 `null`로 둡니다.
- `CharacterSubjectResolver`
  - `raw_entity_mention`이 지칭어이고 `entity_name`이 `미상`/지칭어인 후보를 current chunk 기준으로 묶어 LLM에 전달합니다.
  - previous/current/next chunk 문맥으로 주체만 해소하고, 실패한 placeholder 후보는 저장하지 않도록 제외합니다.
- `SettingCandidateService`
  - 검증된 후보를 `setting_candidates` 저장 모델로 변환합니다.
  - 같은 `analysis_job_id` 기준 기존 후보를 지운 뒤 새 후보를 저장합니다.

현재 단계에서는 검증된 후보를 `setting_candidates` 테이블에 `PENDING_REVIEW` 상태로 저장합니다.
LLM 응답의 JSON 파싱·schema 검증 실패 재시도는 현재 연결되어 있습니다. 동일 인물 병합과 일회성 캐릭터 필터링은 후속 작업에서 다룹니다.

## 로컬 Worker 실행

`AnalysisJobWorker.run_once()`는 분석 작업 하나만 처리하는 함수입니다.
따라서 로컬에서 Worker를 계속 실행하려면 runner script가 반복 호출을 담당합니다.

```bash
.venv/bin/python scripts/run_analysis_worker.py
```

실행 흐름은 다음과 같습니다.

```text
scripts/run_analysis_worker.py
-> AnalysisJobWorker 생성
-> run_once 반복 호출
-> claim할 job이 없으면 idle sleep
-> job이 있으면 청킹/설정 추출/완료 보고 수행
```

수동 확인 시에는 한 번만 claim을 시도할 수 있습니다.

```bash
.venv/bin/python scripts/run_analysis_worker.py --once
```

테스트나 로컬 점검에서는 반복 횟수를 제한할 수 있습니다.

```bash
.venv/bin/python scripts/run_analysis_worker.py --max-iterations 3
```

## 로컬 텍스트 Debug 실행

Spring, DB, S3 없이 로컬 텍스트 파일 하나만으로 청킹부터 설정 후보 추출, 근거 위치 보정,
지칭어 subject fallback, 캐릭터 매칭 상태 계산까지 확인하려면
`scripts/run_episode_text_analysis_debug.py`를 사용합니다.

```bash
.venv/bin/python scripts/run_episode_text_analysis_debug.py \
  --text-file ./samples/episode-1.txt \
  --episode-no 1 \
  --episode-title "1화" \
  --max-chunks 1 \
  --known-characters-json ./samples/known-characters.json \
  --output-json ./tmp/episode-1-debug.json
```

이 runner는 다음 단계만 수행합니다.

```text
로컬 txt 파일 읽기
-> 원문 정규화
-> chunk draft 생성
-> 가상 chunk_id 부여
-> chunk별 설정 후보 LLM 추출
-> evidence quote offset 보정
-> 지칭어 + placeholder 후보 subject fallback
-> knownCharacters 기준 matched_character_id / match_status 계산
-> 콘솔/JSON 파일로 결과 출력
```

`episodeId`, `workId`, `analysisJobId`는 넘기지 않으면 가상 UUID로 생성합니다.
`--known-characters-json`은 Spring claim payload의 `knownCharacters`를 대신하는 입력입니다.
배열 형태 JSON이며 `characterId`, `character_id`, `id` 중 하나와 `name`을 받습니다.

```json
[
  {
    "characterId": "00000000-0000-0000-0000-000000000101",
    "name": "비요른 얀델"
  }
]
```

처음 프롬프트와 offset을 점검할 때는 `--max-chunks 1`로 LLM 호출 범위를 줄입니다.

현재 debug JSON은 최종 청킹 결과와 최종 설정 후보를 저장합니다.
summary에는 subject fallback 호출/해소/폐기 개수가 함께 들어갑니다.
각 `settingCandidates[]`에는 `matched_character_id`, `match_status`, `evidenceMatches`가 포함됩니다.

- LLM 재시도 횟수
- 재시도 실패 사유
- 모델명
- token usage
- chunk별 quote match 실패 개수 요약

위 값은 아직 구조화해서 저장하지 않습니다. 재시도 여부는 실행 중 warning 로그로만 확인할 수 있으므로, 프롬프트/모델 비교 실험을 반복할 때는 debug 출력 확장을 후속으로 검토합니다.
