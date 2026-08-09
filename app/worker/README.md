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

Spring claim API는 프로세스가 처리할 `allowedJobTypes`와 초기 `currentStep`을 받습니다. claim에 성공하면 5분 동안 유효한 lease token과 마지막 checkpoint를 반환합니다.

```text
POST /api/internal/v1/analysis-jobs/claim
```

실행 중에는 다음 API를 사용합니다.

```text
PATCH /api/internal/v1/analysis-jobs/{analysisJobId}/progress
POST  /api/internal/v1/analysis-jobs/{analysisJobId}/heartbeat
POST  /api/internal/v1/analysis-jobs/{analysisJobId}/complete
POST  /api/internal/v1/analysis-jobs/{analysisJobId}/fail
```

모든 변경 요청은 claim 응답의 `X-Worker-Lease-Token`을 보내며, Worker는 provider 호출 중 60초마다 heartbeat로 lease를 갱신합니다. progress의 `currentStep`은 표시용이고 `episodeStatus`와 기계 판독용 `checkpointStage`를 별도 필드로 전달합니다.

```json
{
  "currentStep": "SETTING_EXTRACTION",
  "episodeStatus": "ANALYZING",
  "checkpointStage": "CHARACTER_CANDIDATES_SAVED"
}
```

checkpoint는 `CHUNKS_READY`, `CHARACTER_CANDIDATES_SAVED`, `WORLD_CANDIDATES_PUBLISHED`, `WORLD_COMPARISONS_FINISHED` 순서로만 증가합니다. lease가 만료되어 Job이 다시 claim되면 마지막 checkpoint 이후 stage부터 재개합니다.

## 현재 연결된 실행 흐름

`AnalysisJobWorker.run_once()`는 다음 순서로 한 개의 분석 작업을 처리합니다.

1차 추출 호출은 `LLM_EXTRACTION_MODEL`, 후보와 확정 데이터의 2차 비교 호출은 `LLM_COMPARISON_MODEL`을 사용합니다. 각 값이 비어 있으면 `LLM_MODEL`로 fallback합니다. 초기 회차 Job 안의 세계관 비교와 별도 재비교 Worker는 모두 같은 comparison 모델 설정을 사용합니다.

```text
Spring claim
-> progress 보고
-> characterSettingSchemas를 immutable schema hint로 변환
-> knownCharacters 이름을 추출 prompt 입력으로 준비
-> 단일 episode S3 원문 청킹
-> flag가 켜진 경우에만 저장 청크 batch 임베딩 전 토큰 예약·호출 후 정산
-> chunk별 캐릭터 설정 후보 추출 전 토큰 예약·호출 후 정산
-> evidence quote 위치 보정
-> 구체적이지 않은 entity_name 후보 subject fallback 전 토큰 예약·호출 후 정산
-> setting_candidates 교체 저장
-> 세계관 설정 후보 추출 및 Spring 내부 API 게시
-> 후보별 기존 world_settings 탐색 및 ADD/UPDATE/MERGE/EXCLUDE 비교 저장
-> WORLD_COMPARISONS_FINISHED checkpoint 보고
-> summaryJson 생성
-> Spring complete 보고
```

사용자 재비교는 별도 실행 모드가 담당합니다.

```text
run_analysis_worker.py --worker-kind world-comparison
-> WORLD_SETTING_COMPARISON만 claim
-> 연결된 PENDING 후보 한 건 비교
-> 후보 COMPLETED/FAILED 저장
-> Job complete/fail 보고
```

세부 책임은 다음 파일로 나뉩니다.

- `analysis_job_worker.py`
  - claim된 payload의 단일 episode를 처리합니다.
  - claim의 `characterSettingSchemas`를 Backend가 보낸 순서와 중복을 유지한 immutable schema hint tuple로 job당 한 번 변환해 모든 chunk 추출에 전달합니다.
  - `characterSettingSchemas`가 비어 있으면 등록 schema 기준의 추출을 수행할 수 없으므로, S3 원문 조회와 청크·후보 교체 전에 job을 실패 보고합니다.
  - 해당 episode의 청킹과 chunk별 설정 추출기를 호출하고, feature flag가 켜진 경우에만 임베딩 서비스를 호출합니다.
  - 실제 Worker 실행에서는 호출할 raw LLM·embedding client를 job ID가 결합된 metered wrapper로 감싸고, 테스트에서 주입한 fake service는 그대로 재사용합니다.
  - 생성·생략된 episode/chunk/embedding/candidate 개수를 `summaryJson`으로 모아 Spring에 완료 보고합니다.
- `MeteredTextGenerationClient`, `MeteredEmbeddingClient`
  - 각 provider 호출마다 Spring 원장에 예상 최대 토큰을 먼저 예약합니다.
  - 성공 또는 usage가 포함된 실패는 실제 token 수로 정산하고, usage를 알 수 없는 실패는 예약을 해제합니다.
  - prompt와 응답 본문은 계량 API에 전달하지 않습니다.
  - 자세한 계약은 [usage README](../usage/README.md)를 따릅니다.
- `EpisodeS3ChunkingService`
  - episode_id로 DB의 episode를 조회합니다.
  - episode의 `content_s3_key`로 S3 원문을 읽습니다.
  - 읽은 원문을 `EpisodeChunkService`에 넘겨 기존 chunk 삭제 후 새 chunk 저장을 수행합니다.
- `CharacterSettingExtractor`
  - 저장된 chunk 하나를 LLM에 보내 캐릭터 설정 후보와 명시적 신규 캐릭터 발견 후보를 추출합니다.
  - claim의 `knownCharacters` 대표 이름을 prompt에 전달해 이미 등록된 이름의 발견 후보를 억제합니다. `characterId`는 prompt에 전달하지 않습니다.
  - LLM 응답의 `source_chunk_id`는 사용하지 않고 현재 입력 chunk ID로 강제합니다.
  - LLM 응답 JSON을 `app/analysis/schemas.py` 기준으로 검증합니다.
  - schema hint는 `schemaKey`, `displayName`, `attributePattern`, `aliases`, `valueType` 다섯 필드만 가진 prompt 입력 전용 값입니다.
  - `mergePolicy`, `suggestedOperation`은 LLM에 노출하지 않으며 기존 응답 shape도 변경하지 않습니다.
  - fuzzy alias 매칭이나 schema 자동 생성은 하지 않고, 시간·사건·타임라인 정보와 등록 schema에 대응하지 않는 설정은 후보에서 제외합니다.
- `WorldSettingExtractor`, `WorldSettingCandidateMapper`
  - 같은 회차 chunk에서 지속 가능한 세계관 속성을 한 속성 단위로 추출하고 evidence offset을 보정합니다.
  - 분석 Job 전체에서 구조적으로 같은 후보만 exact dedupe한 뒤 Spring 내부 API로 게시합니다.
- `WorldSettingComparisonPipeline`
  - exact 대상이 없으면 같은 category의 대상명을 `S*` 참조로 좁히고, 최대 3개 상세 문맥을 `T*` 참조로 비교합니다.
  - LLM에는 UUID/version을 노출하지 않으며, Backend의 문맥 stale 응답에는 최신 문맥으로 최대 3회 다시 비교합니다.
  - 후보별 오류는 해당 후보를 `FAILED`로 기록하고 초기 회차 Job의 다른 후보 처리를 계속합니다.
- `WorldSettingComparisonWorker`
  - 공개 recompare 요청이 만든 숨김 `WORLD_SETTING_COMPARISON` Job만 claim합니다.
  - 연결 후보 하나가 성공해야 Job을 완료하고, 후보 비교 실패는 Job 실패로 보고합니다.
- `WorkerLeaseHeartbeat`
  - provider 호출 중 60초마다 현재 Job의 5분 lease를 연장하고, heartbeat 실패를 완료 보고 전에 전파합니다.
- `EpisodeChunkEmbeddingService`
  - `EMBEDDING_GENERATION_ENABLED=true`일 때만 Worker에서 호출합니다. MVP 기본값 `false`에서는 service와 client를 생성하지 않습니다.
  - episode의 저장된 청크 텍스트를 한 번에 임베딩합니다.
  - 벡터와 모델·버전·생성 시각을 `episode_chunks`에 반영합니다.
  - timeout·네트워크·원격 protocol 오류와 HTTP 408/409/429/5xx만 복구 가능한 provider 장애로 분류하며, Worker는 실패 개수를 기록하고 설정 후보 추출을 계속합니다.
  - 요청·인증·응답 계약·데이터 정합성·DB 오류는 Worker가 삼키지 않고 analysis job 실패로 전파합니다.
  - 복구 가능한 provider 장애로 commit되지 않은 임베딩은 `NULL`로 남고 벡터 검색 대상에서 제외됩니다. 자동 backfill은 현재 구현하지 않으므로 완료 요약의 `embeddingFailedChunkCount`로 누락을 확인해야 합니다.
- `evidence_span_resolver.py`
  - LLM이 반환한 `evidence_spans[].quote`를 chunk 원문에서 다시 찾습니다.
  - quote 위치를 `episode_chunks.start_offset`과 더해 회차 전체 기준 offset으로 보정합니다.
  - quote를 찾지 못하면 잘못된 위치를 저장하지 않도록 offset을 `null`로 둡니다.
- `CharacterSubjectResolver`
  - `entity_name`이 비어 있거나 `미상`/지칭어처럼 구체적이지 않은 후보를 raw 표현의 형태와 관계없이 current chunk 기준으로 묶어 LLM에 전달합니다.
  - previous/current/next chunk 문맥으로 주체만 해소하고, 정상 응답으로도 해소하지 못하면 후보를 `미상`으로 보존해 기존 매칭 로직이 `AMBIGUOUS`로 저장하게 합니다.
  - 응답 JSON/schema나 candidate ID 계약이 잘못된 경우에는 사용자 검토 대상으로 숨기지 않고 분석 실패로 전파합니다.
  - `CHARACTER_DISCOVERY`는 이름 자체가 추출 결과이므로 subject fallback 대상에서 제외합니다.
- `SettingCandidateService`
  - 검증된 후보를 `setting_candidates` 저장 모델로 변환합니다.
  - 기존 캐릭터와 매칭된 발견 후보는 제외하고, 같은 분석에서 정규화한 이름이 같은 신규 발견 후보는 첫 근거 하나만 저장합니다.
  - 같은 분석의 `SETTING` 후보 중 주체·설정명·값 타입·구조화 값이 모두 같은 후보는 confidence가 높은 근거 하나만 저장합니다. 실제 값이 다르거나 주체가 모호한 후보는 유지합니다.
  - 같은 `analysis_job_id` 기준 기존 후보를 지운 뒤 새 후보를 저장합니다.

현재 단계에서는 검증된 후보를 `setting_candidates` 테이블에 `PENDING_REVIEW` 상태로 저장합니다. `SETTING`은 기존 설정 값 필드를 사용하고, `CHARACTER_DISCOVERY`는 이름·원문 표현·근거만 저장하며 설정 값 필드를 `NULL`로 둡니다.

## 임베딩 생성 feature flag

MVP는 벡터 검색을 사용하지 않으므로 `EMBEDDING_GENERATION_ENABLED`의 기본값을 `false`로 둡니다. 비활성화된 Worker는 임베딩 service와 OpenAI Embeddings client를 호출하지 않고, 해당 청크 수를 `embeddingSkippedChunkCount`에 기록한 뒤 설정 후보 추출과 Job 완료를 계속합니다.

후속 오류 리포트나 RAG 검색에서 벡터가 필요해지면 환경변수를 `true`로 바꿔 신규 분석과 재분석의 임베딩 생성을 다시 활성화합니다. 기존 `NULL` 임베딩은 자동으로 채워지지 않으므로, 과거 데이터가 필요하면 Spring에서 대상 회차의 재분석 Job을 생성한 뒤 다음처럼 Worker 한 건을 실행하거나 별도 범위 제한 backfill 작업을 추가해야 합니다.

```bash
EMBEDDING_GENERATION_ENABLED=true .venv/bin/python -m scripts.run_analysis_worker --once
```
LLM 응답의 JSON 파싱·schema 검증 실패 재시도는 현재 연결되어 있습니다. 동일 인물 병합과 일회성 캐릭터 필터링은 후속 작업에서 다룹니다.

## 로컬 Worker 실행

`AnalysisJobWorker.run_once()`는 분석 작업 하나만 처리하는 함수입니다.
따라서 로컬에서 Worker를 계속 실행하려면 runner script가 반복 호출을 담당합니다.

```bash
.venv/bin/python -m scripts.run_analysis_worker
```

재비교 queue는 별도 프로세스로 실행합니다.

```bash
.venv/bin/python -m scripts.run_analysis_worker --worker-kind world-comparison
```

실행 흐름은 다음과 같습니다.

```text
scripts/run_analysis_worker.py
-> AnalysisJobWorker 생성
-> run_once 반복 호출
-> claim할 job이 없으면 idle sleep
-> job이 있으면 청킹/설정 추출/완료 보고 수행
-> 개별 job 실패는 Spring에 보고한 뒤 로그와 idle sleep을 남기고 다음 claim 계속
```

수동 확인 시에는 한 번만 claim을 시도할 수 있습니다.

```bash
.venv/bin/python -m scripts.run_analysis_worker --once
```

`--once`에서는 실패 예외가 프로세스 종료 상태로 전달됩니다. 기본 장기 실행 모드에서만 개별 job 실패를 격리하고 다음 claim을 계속합니다.

테스트나 로컬 점검에서는 반복 횟수를 제한할 수 있습니다.

```bash
.venv/bin/python -m scripts.run_analysis_worker --max-iterations 3
```

## 로컬 텍스트 Debug 실행

Spring, DB, S3 없이 로컬 텍스트 파일 하나만으로 청킹부터 설정 후보 추출, 근거 위치 보정,
캐릭터 주체 subject fallback, 캐릭터 매칭 상태 계산까지 확인하려면
`scripts/run_episode_text_analysis_debug.py`를 사용합니다.

```bash
.venv/bin/python scripts/run_episode_text_analysis_debug.py \
  --text-file ./samples/episode-1.txt \
  --episode-no 1 \
  --episode-title "1화" \
  --max-chunks 1 \
  --known-characters-json ./samples/known-characters.json \
  --character-setting-schemas-json ./samples/character-setting-schemas.json \
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
-> 구체적이지 않은 entity_name 후보 subject fallback
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

`--character-setting-schemas-json`은 Spring claim payload의 `characterSettingSchemas`를 대신하는
필수 입력입니다. `schemaKey`, `displayName`, `attributePattern`, `aliases`, `valueType`을 가진
비어 있지 않은 JSON 배열을 실제 claim DTO로 검증한 뒤 입력 순서와 중복을 유지해 모든 chunk의
설정 추출 prompt에 전달합니다.

```json
[
  {
    "schemaKey": "skills.skill",
    "displayName": "스킬",
    "attributePattern": "skill.*",
    "aliases": [],
    "valueType": "JSON"
  }
]
```

처음 프롬프트와 offset을 점검할 때는 `--max-chunks 1`로 LLM 호출 범위를 줄입니다.

현재 debug JSON은 최종 청킹 결과와 최종 설정 후보를 저장합니다.
summary에는 subject fallback 호출/해소/미해소 개수가 함께 들어갑니다.
각 `settingCandidates[]`에는 `matched_character_id`, `match_status`, `evidenceMatches`가 포함됩니다.

- LLM 재시도 횟수
- 재시도 실패 사유
- 모델명
- token usage
- chunk별 quote match 실패 개수 요약

위 값은 아직 구조화해서 저장하지 않습니다. 특히 LLM Client가 응답의 token usage를 읽어도 설정 추출·재시도·subject fallback 결과에서 Worker로 전달하지 않으므로 Spring 완료 보고와 `analysis_jobs` 토큰 컬럼에는 반영되지 않습니다. 재시도 여부는 실행 중 warning 로그로만 확인할 수 있으며, 사용량 집계와 debug 출력 확장은 후속 작업에서 함께 검토합니다.
