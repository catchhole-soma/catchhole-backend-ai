from dataclasses import dataclass

import boto3

from app.core.config import get_settings


@dataclass(frozen=True)
class S3TextObjectStorage:
    bucket: str
    region: str

    @classmethod
    def from_settings(cls) -> "S3TextObjectStorage":
        settings = get_settings()
        return cls(bucket=settings.aws_s3_bucket, region=settings.aws_region)

    def get_text(self, key: str) -> str:
        client = boto3.client("s3", region_name=self.region)
        response = client.get_object(Bucket=self.bucket, Key=key)
        return response["Body"].read().decode("utf-8")
