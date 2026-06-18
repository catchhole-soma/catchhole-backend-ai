from collections.abc import Generator
from functools import lru_cache

from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import get_settings


@lru_cache # 함수 결과를 캐싱
#DB엔진을 만드는 함수
def get_engine() -> Engine:
    settings = get_settings() #core.config의 설정값 읽기
    if not settings.database_url:
        raise RuntimeError("DATABASE_URL is required to create a database engine.")
    return create_engine(settings.database_url, pool_pre_ping=True)


@lru_cache
#Session을 만드는 Sessionmaker 만드는 함수
def get_session_maker() -> sessionmaker[Session]:
    return sessionmaker(
        bind=get_engine(), #어떤 DB엔진을 사용할지
        autocommit=False, 
        autoflush=False,
        expire_on_commit=False,
    )

def get_session() -> Generator[Session, None, None]:
    session = get_session_maker()() #Sessionmaker에서 session을 만든다
    try:
        yield session
    #yield한 응답 완료를 기다렸다가 아래 실행
    finally:
        session.close()
