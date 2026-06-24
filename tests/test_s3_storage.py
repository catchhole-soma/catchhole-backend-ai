from app.core.config import Settings
from app.storage.s3 import S3TextObjectStorage


def test_from_settings_creates_s3_text_storage() -> None:
    settings = Settings(aws_s3_bucket="catchhole-manuscripts", aws_region="ap-northeast-2")

    storage = S3TextObjectStorage.from_settings(settings)

    assert storage.bucket == "catchhole-manuscripts"
    assert storage.region == "ap-northeast-2"


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
