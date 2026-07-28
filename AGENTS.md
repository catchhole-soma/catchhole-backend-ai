# Repository Guidelines

## Pull Requests

- PR을 작성할 때 `.github/pull_request_template.md`의 섹션과 체크리스트를 유지하고 실제 변경에 맞게 모두 채운다.
- 관련 Jira 이슈와 GitHub 이슈·PR을 본문에 연결하고, 리뷰어가 재현할 수 있는 검증 명령과 결과를 참고 사항에 기록한다.

## Spring Worker API

- 분석 progress 요청은 표시용 `currentStep`과 대상 회차에 적용할 `episodeStatus`를 함께 보낸다. 자유 형식 문구에서 상태를 추론하지 않도록 `EpisodeProcessingStatus` enum을 명시적으로 직렬화한다.
- claim payload는 복수 `episodes`가 아니라 단일 `episode`를 받는다. 한 `AnalysisJob`은 한 회차만 처리하고, 회차 사이의 반복과 실패 격리는 Spring의 Job queue가 담당한다.
- 장기 실행 runner는 한 Job의 실패를 Spring에 보고한 뒤 다음 claim을 계속한다. 개별 분석 예외로 Worker 프로세스 전체를 종료하지 않는다.
- `source_chunk_id`는 LLM 생성값이 아니라 Worker가 가진 `EpisodeChunk.id`를 source of truth로 사용한다. LLM 응답에 값이 없거나 잘못되어도 Pydantic 검증 전에 현재 chunk ID로 덮어쓴다.

## AWS S3

- `AWS_ACCESS_KEY_ID`와 `AWS_SECRET_ACCESS_KEY`는 둘 다 설정된 경우에만 boto3 client에 명시적으로 전달하고, `AWS_SESSION_TOKEN`이 있으면 임시 자격 증명의 일부로 함께 전달한다. access key와 secret key가 모두 있지 않으면 기본 credential provider chain을 사용하며 실제 비밀값은 저장소에 커밋하지 않는다.

## Python Packaging

- setuptools package discovery는 `app*`로 제한해 루트의 `samples`, `docs`, `scripts`를 배포 패키지에서 제외한다. `pyproject.toml`이나 루트 디렉터리를 변경하면 `python -m pip install -e ".[dev]"`로 editable install을 검증한다.
