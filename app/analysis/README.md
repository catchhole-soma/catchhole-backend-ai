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
  - prompt 로드, user prompt 구성, JSON 파싱, schema 검증, 검증 실패 재시도를 담당합니다.
- `schemas.py`
  - LLM에서 받은 설정 후보 JSON을 검증하기 위한 Python 내부 schema를 정의합니다.
  - FastAPI 응답 DTO가 아니라, 외부 LLM 출력이 저장 가능한 구조인지 확인하는 경계 객체입니다.
  - 필수 필드 누락, 잘못된 값 타입, 빈 근거 문장 등은 이 단계에서 걸러집니다.
- `exceptions.py`
  - Analysis 내부 흐름에서만 사용하는 예외를 정의합니다.
  - FastAPI 응답용 공통 예외와 분리해 Worker가 분석 실패 사유를 구분할 수 있게 합니다.

## 실패 메시지 처리

현재 LLM 응답 파싱/검증 실패 메시지는 `setting_extractor.py` 내부 helper에서 짧게 정리합니다.

아직 사용처가 `CharacterSettingExtractor` 하나뿐이므로 공통 util로 분리하지 않았습니다.
다만 이후 Worker 실패 보고, Spring 내부 API 실패 보고, S3/DB 처리 실패 등에서 같은 규칙이 필요해지면 `app/core/error_messages.py` 같은 공통 helper로 분리합니다.

## 후속 작업

- `evidence_quote`를 청크 내부 offset으로 다시 매핑합니다.
- 기존 확정 설정과 비교하는 충돌 검사 흐름을 연결합니다.
