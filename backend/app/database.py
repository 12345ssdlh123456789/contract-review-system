# ==========================================================
#  数据库连接与会话管理
# ----------------------------------------------------------
#  SQLite 的坑：默认不允许跨线程使用同一连接。
#  FastAPI 是异步多线程的，所以必须 check_same_thread=False，
#  并且用 SessionLocal() 每个请求一个会话，用完立刻关。
# ==========================================================
from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .config import config
from .models import Base

engine = create_engine(
    config.DB_URL,
    connect_args={"check_same_thread": False},
    echo=False,
    future=True,
)


# 开启 WAL 模式：读写不互相阻塞，多用户同时用时明显更稳
@event.listens_for(engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()


SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)


def get_db() -> Generator[Session, None, None]:
    """FastAPI 依赖注入用。保证请求结束一定关闭会话。"""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db() -> None:
    """建表。生产环境应该换成 Alembic 迁移。"""
    Base.metadata.create_all(bind=engine)
