# storage

외부 object storage 접근을 담당하는 패키지입니다.

Spring 기준으로는 `ObjectStorageService` 같은 외부 저장소 adapter에 가깝습니다.

## 역할

- S3 key를 기준으로 원문 파일을 읽습니다.
- storage client 생성과 호출 세부 구현을 감춥니다.
- 테스트에서는 fake client를 주입해 네트워크 없이 동작을 검증할 수 있게 합니다.

다음 책임은 Storage에 넣지 않습니다.

- Episode 조회
- 청킹 실행
- DB 저장
- LLM 호출
- 분석 작업 상태 변경

## 현재 파일

- `s3.py`
  - `S3TextObjectStorage`를 제공합니다.
  - `get_text(key)`로 S3 object body를 UTF-8 문자열로 읽습니다.
  - 실제 실행에서는 boto3 client를 사용하고, 테스트에서는 fake client를 주입합니다.

## 테스트 기준

현재 테스트는 실제 AWS S3에 접근하지 않습니다.

- fake S3 client로 `get_object(Bucket, Key)` 호출 인자를 확인합니다.
- S3 응답 body를 UTF-8 문자열로 변환하는 흐름을 확인합니다.

실제 AWS credential과 버킷을 사용하는 통합 테스트는 아직 없습니다. 필요해질 경우 별도 integration test로 분리합니다.
