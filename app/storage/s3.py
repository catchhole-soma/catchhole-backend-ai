from dataclasses import dataclass
from typing import Any

import boto3

from app.core.config import Settings, get_settings


@dataclass(frozen=True)
class S3TextObjectStorage:
    bucket: str
    region: str
    client: Any | None = None # client는 아무 객체나 또는 None 가능
    
    @classmethod
    def from_settings(cls, settings: Settings | None = None) -> "S3TextObjectStorage":
        settings = settings or get_settings()
        return cls(bucket=settings.aws_s3_bucket, region=settings.aws_region)

    def get_text(self, key: str) -> str:
        # 테스트에서는 fake client를 주입하고, 실제 실행에서는 boto3 client를 생성한다.
        client = self.client or boto3.client("s3", region_name=self.region)
        response = client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read().decode("utf-8")
