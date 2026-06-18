from app.models import AnalysisJob, Base, Episode, UploadBatch, UploadFile, Work


def test_first_issue_models_are_registered() -> None:
    table_names = set(Base.metadata.tables.keys())

    assert AnalysisJob.__tablename__ in table_names
    assert Episode.__tablename__ in table_names
    assert UploadBatch.__tablename__ in table_names
    assert UploadFile.__tablename__ in table_names
    assert Work.__tablename__ in table_names


def test_analysis_job_columns_match_first_issue_scope() -> None:
    columns = set(AnalysisJob.__table__.columns.keys())

    assert {
        "id",
        "work_id",
        "batch_id",
        "episode_id",
        "job_type",
        "status",
        "current_step",
        "error_message",
        "started_at",
        "completed_at",
    }.issubset(columns)
