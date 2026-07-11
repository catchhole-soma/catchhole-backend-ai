from sqlalchemy import BigInteger

from app.models.episode_chunk import EpisodeChunk
from app.models.setting_candidate import SettingCandidate
from app.models.upload_batch import UploadBatch
from app.models.upload_file import UploadFile
from app.models.work import Work


def test_bigint_columns_match_flyway_schema() -> None:
    assert isinstance(Work.__table__.c.member_id.type, BigInteger)
    assert isinstance(UploadBatch.__table__.c.member_id.type, BigInteger)
    assert isinstance(UploadFile.__table__.c.file_size.type, BigInteger)


def test_removed_work_status_is_not_mapped() -> None:
    assert "status" not in Work.__table__.c


def test_java_owned_timestamps_are_not_nullable() -> None:
    assert SettingCandidate.__table__.c.created_at.nullable is False
    assert SettingCandidate.__table__.c.updated_at.nullable is False
    assert SettingCandidate.__table__.c.created_at.default is not None
    assert SettingCandidate.__table__.c.updated_at.default is not None


def test_episode_chunk_timestamps_are_not_nullable() -> None:
    assert EpisodeChunk.__table__.c.created_at.nullable is False
    assert EpisodeChunk.__table__.c.updated_at.nullable is False
    assert EpisodeChunk.__table__.c.created_at.default is not None
    assert EpisodeChunk.__table__.c.updated_at.default is not None
