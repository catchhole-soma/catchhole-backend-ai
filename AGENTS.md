# Repository Guidelines

## Pull Requests

- PR을 작성할 때 `.github/pull_request_template.md`의 섹션과 체크리스트를 유지하고 실제 변경에 맞게 모두 채운다.
- 관련 Jira 이슈와 GitHub 이슈·PR을 본문에 연결하고, 리뷰어가 재현할 수 있는 검증 명령과 결과를 참고 사항에 기록한다.

## Spring Worker API

- 분석 runner는 claim의 `allowedJobTypes`를 명시한다. 기본 `analysis` 프로세스는 `SETTING_EXTRACTION`, 별도 `world-comparison` 프로세스는 사용자 재비교용 `WORLD_SETTING_COMPARISON`만 claim해 서로의 작업을 가져가지 않는다.
- claim 뒤 상태 변경, checkpoint, 세계관 후보 API와 토큰 예약에는 `X-Worker-Lease-Token`을 전송하고, 장기 provider 호출 중에는 60초 주기로 heartbeat를 보낸다. lease가 만료된 응답을 우회해 DB 상태를 직접 바꾸지 않는다.
- `SETTING_EXTRACTION`의 재시작 경계는 `CHUNKS_READY → CHARACTER_CANDIDATES_SAVED → WORLD_CANDIDATES_PUBLISHED → WORLD_COMPARISONS_FINISHED` checkpoint 순서를 사용한다. 완료된 stage의 외부 호출과 저장을 반복하지 않는다.
- 캐릭터 `setting_candidates` 저장은 기존 SQLAlchemy 경계를 유지한다. 세계관 `world_setting_candidates` 생성·비교 상태 저장은 반드시 Spring 내부 Worker API를 사용하며, Python이 `world_settings`나 세계관 후보 테이블을 직접 수정하지 않는다.
- 세계관 비교 prompt에는 Backend UUID를 노출하지 않는다. Worker가 만든 `S*`/`T*` 참조만 LLM에 제공하고, 실제 대상 ID·현재 property·version 검증과 `beforeValue` 산출은 Spring이 담당한다.
- 세계관 `comparisonReason`은 검토 화면에 그대로 노출되는 사용자 문장이다. `S*`/`T*` 참조, UUID, key, version, operation enum 같은 내부 용어를 저장하지 않으며, 모델이 대상 참조를 반환하면 실제 대상명을 사용한 자연스러운 한국어로 치환한다.
- 기존 속성과 의미가 같아 세계관 후보를 `EXCLUDE`할 때는 2차 비교 결과에 해당 `target_ref`와 실제 `matched_property_name`을 함께 반환한다. Backend가 비교 당시 기존값을 `beforeValue`로 보존해야 하며, 일시적 사건처럼 특정 기존 속성과 비교하지 않은 제외만 두 값을 비울 수 있다.
- 분석 progress 요청은 표시용 `currentStep`과 대상 회차에 적용할 `episodeStatus`를 함께 보낸다. 자유 형식 문구에서 상태를 추론하지 않도록 `EpisodeProcessingStatus` enum을 명시적으로 직렬화한다.
- claim payload는 복수 `episodes`가 아니라 단일 `episode`를 받는다. 한 `AnalysisJob`은 한 회차만 처리하고, 회차 사이의 반복과 실패 격리는 Spring의 Job queue가 담당한다.
- 장기 실행 runner는 한 Job의 실패를 Spring에 보고한 뒤 다음 claim을 계속한다. 개별 분석 예외로 Worker 프로세스 전체를 종료하지 않는다.
- `source_chunk_id`는 LLM 생성값이 아니라 Worker가 가진 `EpisodeChunk.id`를 source of truth로 사용한다. LLM 응답에 값이 없거나 잘못되어도 Pydantic 검증 전에 현재 chunk ID로 덮어쓴다.
- 설정 추출 prompt에는 claim의 `knownCharacters` 대표 이름만 전달하고 Backend 내부 매칭용 `characterId`는 노출하지 않는다. 원문에 명시된 미등록 이름은 `candidate_kind=CHARACTER_DISCOVERY`로 추출하고 설정 payload는 모두 `null`로 두며, 기존 이름과 매칭되는 발견 후보와 같은 분석 안의 중복 발견은 저장 전에 제외한다.
- `CHARACTER_DISCOVERY`의 캐릭터 매칭은 `entity_name`만 기준으로 한다. `케닉의 넷째 아들 세룸` 같은 `raw_entity_mention` 안의 기존 관계자 이름을 발견 대상 캐릭터로 오연결하거나 subject fallback으로 재해석하지 않는다.
- 같은 분석 작업의 `SETTING` 후보는 확정된 캐릭터 ID 또는 정규화한 구체 이름, `attribute_name`, `value_type`, canonical `value_json`이 모두 같을 때만 저장 전에 중복 제거하고 더 높은 confidence의 근거를 남긴다. 값이 다르거나 주체가 `AMBIGUOUS`인 후보는 변화·다른 인물 가능성이 있으므로 유지한다.
- `SettingCandidate.value_json`은 `JSONB(none_as_null=True)`로 매핑한다. `CHARACTER_DISCOVERY`의 Python `None`은 JSON literal `null`이 아니라 DB check constraint가 요구하는 SQL `NULL`로 저장해야 한다.

## Async Worker Runtime

- 장기 실행 runner는 `AI_WORKER_CONCURRENCY`개의 실행 슬롯만 유지한다. 반드시 빈 슬롯을 확보한 뒤 Job 하나를 claim해 즉시 Task로 실행하고, 슬롯 없이 Job을 미리 claim해 프로세스 내부 대기열에 쌓지 않는다.
- 한 Job 안의 청크와 분석 stage는 순차 처리한다. `LLM_MAX_CONCURRENT_REQUESTS`는 프로세스 내부 provider 호출 상한이고, 동기 DB/S3 작업은 `AI_WORKER_BLOCKING_MAX_WORKERS`로 제한한 executor에 넘긴다.
- 운영 `SETTING_EXTRACTION` 검증 rollout은 분석 Worker 2개 × 프로세스당 동시 Job 5개 = 최대 10개다. 별도 `world-comparison` 프로세스는 Job·LLM 동시성을 1로 유지한다. 10은 설정 추출 Job 용량이며 여러 프로세스와 재비교 Worker를 합친 provider 계정 전체의 분산 상한은 아니다. 50개 Job 부하 테스트에서 Backend·PostgreSQL·LLM 지표를 확인하고 기준 미달이면 프로세스당 3개로 되돌린다.
- 각 Job의 lease token, heartbeat, 토큰 예약·정산, 실패 상태는 Task별로 분리한다. 한 Task의 예외가 실행 중인 다른 Job을 취소하지 않으며 heartbeat도 Job별 독립 Task로 실행한다.
- 종료 신호를 받으면 신규 claim을 즉시 중단하고 `AI_WORKER_SHUTDOWN_GRACE_SECONDS` 동안 실행 중 Job과 heartbeat를 유지한다. 운영 내부 grace는 180초, Compose `stop_grace_period`는 210초로 두며, grace를 넘긴 취소 Job은 heartbeat를 중단해 Spring의 lease 회수 경로로 재처리한다.

## AWS S3

- `AWS_ACCESS_KEY_ID`와 `AWS_SECRET_ACCESS_KEY`는 둘 다 설정된 경우에만 boto3 client에 명시적으로 전달하고, `AWS_SESSION_TOKEN`이 있으면 임시 자격 증명의 일부로 함께 전달한다. access key와 secret key가 모두 있지 않으면 기본 credential provider chain을 사용하며 실제 비밀값은 저장소에 커밋하지 않는다.

## Python Packaging

- setuptools package discovery는 `app*`로 제한해 루트의 `samples`, `docs`, `scripts`를 배포 패키지에서 제외한다. `pyproject.toml`이나 루트 디렉터리를 변경하면 `python -m pip install -e ".[dev]"`로 editable install을 검증한다.

## Runtime Timezone

- 운영 AI Worker는 Backend와 PostgreSQL의 `APP_TIMEZONE`을 `TZ`로 전달받으며 기본값은 `Asia/Seoul`이다. Python의 `datetime.now()`와 timezone 없는 공유 DB 컬럼이 동일한 로컬 시각을 사용하도록 이미지의 `tzdata`를 유지한다.

## Embedding Generation

- 신규 청크 임베딩 생성은 `EMBEDDING_GENERATION_ENABLED`로 제어하며 MVP 기본값은 `false`다. 비활성화 시 Embeddings client를 생성·호출하지 않고 설정 후보 추출과 Job 완료를 계속하며, pgvector schema와 임베딩 service·검색 코드는 후속 재활성화를 위해 유지한다.

## LLM Runtime

- 캐릭터 Fact·세계관 후보의 1차 추출은 `LLM_EXTRACTION_MODEL`, 캐릭터·세계관 주체 해소는 `LLM_SUBJECT_RESOLUTION_MODEL`, 후보와 확정 데이터 비교는 `LLM_COMPARISON_MODEL`로 독립 주입한다. 운영 기본 라우팅은 추출 `gpt-5.6-terra`, 주체 해소·비교 `gpt-5.6-luna`이며 개별 값이 없으면 기존 `LLM_MODEL`을 fallback으로 사용한다.
- 세계관 2차 비교·재비교 prompt에는 Backend가 반환한 1차 `evidenceSpans`를 읽기 전용 문맥으로 전달한다. 2차 LLM이 quote·offset을 다시 생성하거나 비교 완료 payload로 반환하지 않으며, 원고가 바뀐 경우에만 새 1차 분석 후보와 근거를 만든다.
- 세계관 후보는 Spring 게시 전에 정규화한 `category + subject_name + scope_name + setting_name`별로 하나로 통합한다. `scope_name`은 세계관에만 있는 선택적 1단계 범위이며 빈 값은 루트 property를 뜻한다. 같은 설정명이라도 범위가 다르면 통합하지 않고, 2차 비교도 반드시 범위+설정명 전체 경로를 정확히 매칭한다. 2차 비교는 추출값 하나면 `SINGLE`, 여러 값이 양립하면 `MERGED`, 동시에 참일 수 없으면 `CONFLICT`로 판정한다. `MERGED`만 자연스러운 최종 문자열 하나로 정리하고 `CONFLICT`는 모든 추출값을 그대로 보존해 사용자 판단으로 넘긴다. 각 1차 후보의 quote·offset과 raw payload는 어느 상태에서도 수정하지 않는다.
- 공통 추론 강도는 `LLM_REASONING_EFFORT`로 주입한다. GPT-5.6 Terra·Luna의 MVP 기준 추론 강도는 `none`이며, 모델 평가 없이 provider 기본값에 의존하지 않는다.
- GPT-5.6 모델의 토큰 예약량은 `o200k_base` tokenizer로 계산한다. 사용하는 tiktoken 버전이 모델 별칭을 모를 수 있으므로 모델명 자동 탐지 실패를 byte 상한으로 방치하지 않는다.
