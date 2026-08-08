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
- 분석 progress 요청은 표시용 `currentStep`과 대상 회차에 적용할 `episodeStatus`를 함께 보낸다. 자유 형식 문구에서 상태를 추론하지 않도록 `EpisodeProcessingStatus` enum을 명시적으로 직렬화한다.
- claim payload는 복수 `episodes`가 아니라 단일 `episode`를 받는다. 한 `AnalysisJob`은 한 회차만 처리하고, 회차 사이의 반복과 실패 격리는 Spring의 Job queue가 담당한다.
- 장기 실행 runner는 한 Job의 실패를 Spring에 보고한 뒤 다음 claim을 계속한다. 개별 분석 예외로 Worker 프로세스 전체를 종료하지 않는다.
- `source_chunk_id`는 LLM 생성값이 아니라 Worker가 가진 `EpisodeChunk.id`를 source of truth로 사용한다. LLM 응답에 값이 없거나 잘못되어도 Pydantic 검증 전에 현재 chunk ID로 덮어쓴다.
- 설정 추출 prompt에는 claim의 `knownCharacters` 대표 이름만 전달하고 Backend 내부 매칭용 `characterId`는 노출하지 않는다. 원문에 명시된 미등록 이름은 `candidate_kind=CHARACTER_DISCOVERY`로 추출하고 설정 payload는 모두 `null`로 두며, 기존 이름과 매칭되는 발견 후보와 같은 분석 안의 중복 발견은 저장 전에 제외한다.
- `CHARACTER_DISCOVERY`의 캐릭터 매칭은 `entity_name`만 기준으로 한다. `케닉의 넷째 아들 세룸` 같은 `raw_entity_mention` 안의 기존 관계자 이름을 발견 대상 캐릭터로 오연결하거나 subject fallback으로 재해석하지 않는다.
- 같은 분석 작업의 `SETTING` 후보는 확정된 캐릭터 ID 또는 정규화한 구체 이름, `attribute_name`, `value_type`, canonical `value_json`이 모두 같을 때만 저장 전에 중복 제거하고 더 높은 confidence의 근거를 남긴다. 값이 다르거나 주체가 `AMBIGUOUS`인 후보는 변화·다른 인물 가능성이 있으므로 유지한다.
- `SettingCandidate.value_json`은 `JSONB(none_as_null=True)`로 매핑한다. `CHARACTER_DISCOVERY`의 Python `None`은 JSON literal `null`이 아니라 DB check constraint가 요구하는 SQL `NULL`로 저장해야 한다.

## AWS S3

- `AWS_ACCESS_KEY_ID`와 `AWS_SECRET_ACCESS_KEY`는 둘 다 설정된 경우에만 boto3 client에 명시적으로 전달하고, `AWS_SESSION_TOKEN`이 있으면 임시 자격 증명의 일부로 함께 전달한다. access key와 secret key가 모두 있지 않으면 기본 credential provider chain을 사용하며 실제 비밀값은 저장소에 커밋하지 않는다.

## Python Packaging

- setuptools package discovery는 `app*`로 제한해 루트의 `samples`, `docs`, `scripts`를 배포 패키지에서 제외한다. `pyproject.toml`이나 루트 디렉터리를 변경하면 `python -m pip install -e ".[dev]"`로 editable install을 검증한다.

## Runtime Timezone

- 운영 AI Worker는 Backend와 PostgreSQL의 `APP_TIMEZONE`을 `TZ`로 전달받으며 기본값은 `Asia/Seoul`이다. Python의 `datetime.now()`와 timezone 없는 공유 DB 컬럼이 동일한 로컬 시각을 사용하도록 이미지의 `tzdata`를 유지한다.

## Embedding Generation

- 신규 청크 임베딩 생성은 `EMBEDDING_GENERATION_ENABLED`로 제어하며 MVP 기본값은 `false`다. 비활성화 시 Embeddings client를 생성·호출하지 않고 설정 후보 추출과 Job 완료를 계속하며, pgvector schema와 임베딩 service·검색 코드는 후속 재활성화를 위해 유지한다.

## LLM Runtime

- 1차 후보 추출은 `LLM_EXTRACTION_MODEL`, 2차 확정 데이터 비교는 `LLM_COMPARISON_MODEL`로 독립 주입한다. 개별 값이 없으면 기존 `LLM_MODEL`을 fallback으로 사용해 이전 배포 환경을 유지한다. 이 단계명은 세계관에 종속하지 않으며 추후 캐릭터 비교에도 같은 비교 모델 설정을 사용한다.
- 공통 추론 강도는 `LLM_REASONING_EFFORT`로 주입한다. GPT-5.6 Terra의 MVP 기준 추론 강도는 `none`이며, 모델 평가 없이 provider 기본값에 의존하지 않는다.
- GPT-5.6 모델의 토큰 예약량은 `o200k_base` tokenizer로 계산한다. 사용하는 tiktoken 버전이 모델 별칭을 모를 수 있으므로 모델명 자동 탐지 실패를 byte 상한으로 방치하지 않는다.
