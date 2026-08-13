from pgvector.sqlalchemy import VECTOR
from sqlalchemy import BigInteger, DateTime, Text

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


def test_setting_candidate_kind_and_nullable_value_columns_match_flyway_schema() -> None:
    assert SettingCandidate.__table__.c.candidate_kind.nullable is False
    assert SettingCandidate.__table__.c.candidate_kind.type.length == 30
    assert SettingCandidate.__table__.c.attribute_name.nullable is True
    assert SettingCandidate.__table__.c.value_type.nullable is True
    assert SettingCandidate.__table__.c.value_json.type.none_as_null is True


def test_setting_candidate_character_comparison_columns_match_flyway_schema() -> None:
    columns = SettingCandidate.__table__.c

    assert columns.comparison_status.nullable is False
    assert columns.comparison_status.type.length == 40
    assert columns.suggested_operation.type.length == 30
    assert columns.temporal_scope.type.length == 30
    assert columns.comparison_target_fact_type.type.length == 30
    assert columns.comparison_target_fact_key.type.length == 150
    assert isinstance(columns.proposed_fact_value.type, Text)
    assert columns.proposed_value_json.type.none_as_null is True
    assert isinstance(columns.comparison_base_snapshot_version.type, BigInteger)
    assert columns.comparison_context_hash.type.length == 64
    assert isinstance(columns.compared_at.type, DateTime)
    assert columns.compared_at.type.timezone is False
    for column_name in (
        "suggested_operation",
        "temporal_scope",
        "comparison_target_fact_type",
        "comparison_target_fact_key",
        "proposed_fact_value",
        "proposed_value_json",
        "removed_snapshot_entries_json",
        "comparison_reason",
        "comparison_base_snapshot_version",
        "comparison_context_hash",
        "raw_comparison_json",
        "compared_at",
        "comparison_error_message",
    ):
        assert columns[column_name].nullable is True


def test_episode_chunk_timestamps_are_not_nullable() -> None:
    assert EpisodeChunk.__table__.c.created_at.nullable is False
    assert EpisodeChunk.__table__.c.updated_at.nullable is False
    assert EpisodeChunk.__table__.c.created_at.default is not None
    assert EpisodeChunk.__table__.c.updated_at.default is not None


def test_episode_chunk_embedding_columns_match_flyway_schema() -> None:
    embedding_column = EpisodeChunk.__table__.c.embedding

    assert isinstance(embedding_column.type, VECTOR)
    assert embedding_column.type.dim == 1536
    assert embedding_column.nullable is True
    assert EpisodeChunk.__table__.c.embedding_model.type.length == 100
    assert EpisodeChunk.__table__.c.embedding_version.type.length == 50
    assert EpisodeChunk.__table__.c.embedded_at.nullable is True
