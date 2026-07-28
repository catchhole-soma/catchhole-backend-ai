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
  - `AWS_ACCESS_KEY_ID`와 `AWS_SECRET_ACCESS_KEY`가 둘 다 있으면 client에 명시적으로 전달하고, `AWS_SESSION_TOKEN`도 있으면 임시 자격 증명의 일부로 함께 전달합니다. access key와 secret key 중 하나라도 없으면 boto3 기본 credential provider chain을 사용합니다.

실제 자격 증명은 `.env`나 배포 환경의 secret으로 주입하며 저장소에 커밋하지 않습니다. 로컬에서 AWS CLI profile, EC2 IAM role, ECS task role처럼 boto3가 기본으로 인식하는 자격 증명을 사용한다면 세 환경 변수는 비워 둡니다.

## 테스트 기준

현재 테스트는 실제 AWS S3에 접근하지 않습니다.

- fake S3 client로 `get_object(Bucket, Key)` 호출 인자를 확인합니다.
- S3 응답 body를 UTF-8 문자열로 변환하는 흐름을 확인합니다.
- 두 자격 증명이 모두 설정된 경우에만 boto3 client 생성 인자로 전달되는지 확인합니다.
- 임시 자격 증명에 세션 토큰이 있으면 boto3 client 생성 인자에 함께 전달되는지 확인합니다.

실제 AWS credential과 버킷을 사용하는 통합 테스트는 아직 없습니다. 필요해질 경우 별도 integration test로 분리합니다.
