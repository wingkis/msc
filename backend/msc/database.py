"""Database engine and session management."""


from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from msc.config import get_settings

settings = get_settings()

# Ensure data directory exists
settings.db_path.parent.mkdir(parents=True, exist_ok=True)

engine = create_engine(
    f"sqlite:///{settings.db_path}",
    # timeout: a LAN host (agent CLIs, maintenance scripts) may hold the file
    # briefly; wait rather than fail with "disk I/O error".
    # pool_recycle: pooled connections that outlive a WAL hiccup are rebuilt.
    connect_args={"check_same_thread": False, "timeout": 30},
    pool_recycle=600,
    echo=False,
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def get_db():
    """FastAPI dependency for database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
