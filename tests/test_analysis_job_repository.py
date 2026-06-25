from uuid import uuid4

import pytest

from app.exceptions.app_exception import AppException
from app.repositories.analysis_job_repository import AnalysisJobRepository


def test_get_by_id_or_throw_raises_when_job_missing(fake_session) -> None:
    repository = AnalysisJobRepository(fake_session)
    missing_id = uuid4()

    with pytest.raises(AppException) as exc_info:
        repository.get_by_id_or_throw(missing_id)

    assert exc_info.value.detail == {"analysis_job_id": str(missing_id)}


class FakeSession:
    def get(self, model, primary_key):
        return None


@pytest.fixture
def fake_session() -> FakeSession:
    return FakeSession()
