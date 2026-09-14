"""SQLAlchemy 2によるPostgreSQL接続の構築。"""

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine


def create_session_factory(database_url: str) -> async_sessionmaker[AsyncSession]:
    """共有せずリクエスト単位に生成するAsyncSession factoryを返す。"""

    engine = create_async_engine(database_url, pool_pre_ping=True)
    return async_sessionmaker(engine, expire_on_commit=False)
