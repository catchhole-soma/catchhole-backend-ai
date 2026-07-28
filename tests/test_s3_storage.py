from typing import Any

import pytest

from app.core.config import Settings
from app.storage.s3 import S3TextObjectStorage


def test_from_settings_creates_s3_text_storage() -> None:
    settings = Settings(
        _env_file=None,
        aws_s3_bucket="catchhole-manuscripts",
        aws_region="ap-northeast-2",
        aws_access_key_id="test-access-key",
        aws_secret_access_key="test-secret-key",
        aws_session_token="test-session-token",
    )

    storage = S3TextObjectStorage.from_settings(settings)

    assert storage.bucket == "catchhole-manuscripts"
    assert storage.region == "ap-northeast-2"
    assert storage.access_key_id == "test-access-key"
    assert storage.secret_access_key == "test-secret-key"
    assert storage.session_token == "test-session-token"


def test_get_text_creates_s3_client_with_settings_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client(body_text="자격 증명 테스트 원고입니다.")
    request: dict[str, Any] = {}

    def create_client(service_name: str, **kwargs: Any) -> FakeS3Client:
        request["service_name"] = service_name
        request.update(kwargs)
        return client

    monkeypatch.setattr("app.storage.s3.boto3.client", create_client)
    storage = S3TextObjectStorage.from_settings(
        Settings(
            _env_file=None,
            aws_s3_bucket="catchhole-manuscripts",
            aws_region="ap-northeast-2",
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
        )
    )

    storage.get_text("works/work-id/episodes/episode-id.txt")

    assert request == {
        "service_name": "s3",
        "region_name": "ap-northeast-2",
        "aws_access_key_id": "test-access-key",
        "aws_secret_access_key": "test-secret-key",
    }


def test_get_text_preserves_session_token_with_temporary_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client(body_text="임시 자격 증명 테스트 원고입니다.")
    request: dict[str, Any] = {}

    def create_client(service_name: str, **kwargs: Any) -> FakeS3Client:
        request["service_name"] = service_name
        request.update(kwargs)
        return client

    monkeypatch.setattr("app.storage.s3.boto3.client", create_client)
    storage = S3TextObjectStorage.from_settings(
        Settings(
            _env_file=None,
            aws_s3_bucket="catchhole-manuscripts",
            aws_region="ap-northeast-2",
            aws_access_key_id="test-access-key",
            aws_secret_access_key="test-secret-key",
            aws_session_token="test-session-token",
        )
    )

    storage.get_text("works/work-id/episodes/episode-id.txt")

    assert request == {
        "service_name": "s3",
        "region_name": "ap-northeast-2",
        "aws_access_key_id": "test-access-key",
        "aws_secret_access_key": "test-secret-key",
        "aws_session_token": "test-session-token",
    }


def test_get_text_reads_utf8_text_from_s3_object() -> None:
    client = FakeS3Client(body_text="첫 번째 원고입니다.")
    storage = S3TextObjectStorage(
        bucket="catchhole-manuscripts",
        region="ap-northeast-2",
        client=client,
    )

    text = storage.get_text("works/work-id/episodes/episode-id.txt")

    assert text == "첫 번째 원고입니다."
    assert client.request == {
        "Bucket": "catchhole-manuscripts",
        "Key": "works/work-id/episodes/episode-id.txt",
    }


class FakeBody:
    # boto3 get_object 응답의 Body.read() 흐름을 흉내 내는 테스트용 객체다.
    def __init__(self, text: str) -> None:
        self.text = text

    def read(self) -> bytes:
        return self.text.encode("utf-8")


class FakeS3Client:
    # 실제 S3 대신 get_object 호출 인자와 반환 본문을 기록하는 테스트용 객체다.
    def __init__(self, body_text: str) -> None:
        self.body_text = body_text
        self.request: dict[str, str] | None = None

    def get_object(self, Bucket: str, Key: str) -> dict[str, FakeBody]:
        self.request = {"Bucket": Bucket, "Key": Key}
        return {"Body": FakeBody(self.body_text)}
