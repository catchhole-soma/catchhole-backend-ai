from app.core.config import get_settings
from app.db.session import get_engine, get_session_maker

#ATABASE_URL 환경변수에 넣은 값으로 실제 엔진이 만들어지는지 확인
def test_get_engine_uses_database_url(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    _clear_settings_and_db_cache() #@lru_cache로 만들어진 이전 엔진을 초기화

    engine = get_engine()

    assert str(engine.url) == "sqlite+pysqlite:///:memory:"
    _clear_settings_and_db_cache()  

#get_session_maker()가 캐시되어서 같은 객체를 반환하는지 확인
def test_get_session_maker_is_cached(monkeypatch) -> None:
    monkeypatch.setenv("DATABASE_URL", "sqlite+pysqlite:///:memory:")
    _clear_settings_and_db_cache()

    first_session_maker = get_session_maker()
    second_session_maker = get_session_maker()

    assert first_session_maker is second_session_maker
    _clear_settings_and_db_cache()

#캐시 삭제 함수
def _clear_settings_and_db_cache() -> None:
    get_settings.cache_clear()
    get_engine.cache_clear()
    get_session_maker.cache_clear()
