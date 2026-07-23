# Repository Guidelines

## Pull Requests

- PR을 작성할 때 `.github/pull_request_template.md`의 섹션과 체크리스트를 유지하고 실제 변경에 맞게 모두 채운다.
- 관련 Jira 이슈와 GitHub 이슈·PR을 본문에 연결하고, 리뷰어가 재현할 수 있는 검증 명령과 결과를 참고 사항에 기록한다.

## Spring Worker API

- 분석 progress 요청은 표시용 `currentStep`과 대상 회차에 적용할 `episodeStatus`를 함께 보낸다. 자유 형식 문구에서 상태를 추론하지 않도록 `EpisodeProcessingStatus` enum을 명시적으로 직렬화한다.
