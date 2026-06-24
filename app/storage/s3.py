from dataclasses import dataclass
from typing import Any

import boto3

from app.core.config import Settings, get_settings

# S3에서 텍스트 파일을 읽어오는 Storage 객체
@dataclass(frozen=True)
class S3TextObjectStorage:
    bucket: str # S3 버킷 이름
    region: str # AWS 리전
    client: Any | None = None # S3 client 객체, client는 아무 객체나 또는 None 가능
    
    
    
    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "S3TextObjectStorage":
        """
        - 역할: 전역 Settings를 주입받아 객체를 조립하는 '정적 팩토리 메서드'
        - @classmethod 사용 이유: 
          자바의 static 메서드처럼 호출하지만, 첫 번째 인자로 클래스 자체(cls)를 주입받습니다.
          이로 인해 이 클래스를 상속한 자식 클래스에서 본 메서드를 호출하더라도 
          부모가 아닌 자식 클래스의 인스턴스를 동적(다형성)으로 올바르게 반환합니다.
        """
        settings = settings or get_settings()
        # cls는 현재 클래스, 즉 S3TextObjectStorage를 의미
        return cls(bucket=settings.aws_s3_bucket, region=settings.aws_region)

    # S3 object key를 받아 해당 파일 내용을 문자열로 반환
    def get_text(self, key: str) -> str:
        # 테스트에서는 fake client를 주입하고, 실제 실행에서는 boto3 client를 생성
        client = self.client or boto3.client("s3", region_name=self.region)
        # S3에서 객체를 가져옴
        response = client.get_object(Bucket=self.bucket, Key=key)
        # read()로 bytes를 읽고, decode("utf-8")로 문자열로 변환
        return response["Body"].read().decode("utf-8")
