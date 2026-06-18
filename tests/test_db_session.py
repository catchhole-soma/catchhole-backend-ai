from app.core.config import get_settings
from app.db.session import get_engine, get_session_maker


def test_get_engine_uses_database_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    _clear_settings_and_db_cache()

    engine = get_engine()

    assert str(engine.url) == "sqlite+pysqlite:///:memory:"
    _clear_settings_and_db_cache()


def test_get_session_maker_is_cached(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    _clear_settings_and_db_cache()

    first_session_maker = get_session_maker()
    second_session_maker = get_session_maker()

    assert first_session_maker is second_session_maker
    _clear_settings_and_db_cache()


def _clear_settings_and_db_cache() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_maker.cache_clear()
