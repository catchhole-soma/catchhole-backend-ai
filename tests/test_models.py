from app.models import AnalysisJob, Base, Episode, UploadBatch, UploadFile, Work

#각 모델의 테이블 이름이 Base.metadata에 등록되어 있는지 확인
def test_first_issue_models_are_registered() -> None:
    table_names = set(Base.metadata.tables.keys())

    assert AnalysisJob.__tablename__ in table_names
    assert Episode.__tablename__ in table_names
    assert UploadBatch.__tablename__ in table_names
    assert UploadFile.__tablename__ in table_names
    assert Work.__tablename__ in table_names

#AnalysisJob 테이블에 특정 컬럼들이 존재하는지 확인하는 테스트
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
