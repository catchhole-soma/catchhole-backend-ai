# analysis

AI 분석 유스케이스와 분석 판단 로직을 두는 패키지입니다.

Spring 기준으로는 여러 하위 기능을 조합해 도메인 분석 결과를 만드는 Service/Domain Service에 가깝습니다.

## 역할

- 원문 청크를 입력으로 받아 설정 후보를 추출합니다.
- LLM 응답 JSON을 Python 내부 검증 schema로 확인합니다.
- 추출 결과를 `setting_candidates` 저장 구조에 맞는 중간 결과로 정리합니다.
- 후속 단계에서 근거 위치 계산, 충돌 검사, 요약 생성 로직을 연결합니다.

다음 책임은 Analysis에 넣지 않습니다.

- OpenAI HTTP 호출 세부 구현
- S3 원문 조회
- SQLAlchemy query 세부 작성
- Spring 내부 Worker API 호출

## 현재 파일

- `setting_extractor.py`
  - 청크 하나를 LLM에 보내 캐릭터 설정 후보를 추출합니다.
  - prompt 로드, user prompt 구성, JSON 파싱, schema 검증을 담당합니다.
- `schemas.py`
  - LLM에서 받은 설정 후보 JSON을 검증하기 위한 Python 내부 schema를 정의합니다.
  - FastAPI 응답 DTO가 아니라, 외부 LLM 출력이 저장 가능한 구조인지 확인하는 경계 객체입니다.

## 후속 작업

- `evidence_quote`를 청크 내부 offset으로 다시 매핑합니다.
- LLM 응답 JSON 검증 실패 시 재시도 정책을 추가합니다.
- 기존 확정 설정과 비교하는 충돌 검사 흐름을 연결합니다.
