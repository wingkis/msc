"""Database engine and session management."""


from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from sqlalchemy.pool import NullPool

from msc.config import get_settings

settings = get_settings()

# Ensure data directory exists
settings.db_path.parent.mkdir(parents=True, exist_ok=True)

# NullPool: every request gets a fresh connection, closed at request end.
# The recurring "disk I/O error" incidents came from long-lived pooled
# connections holding a stale WAL reference while host-side agent scripts
# touched the same file. At LAN scale the per-request connection cost is
# negligible, and it removes the poisoning entirely.
engine = create_engine(
    f"sqlite:///{settings.db_path}",
    connect_args={"check_same_thread": False},
    poolclass=NullPool,
    echo=False,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, connection_record):
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    # Wait up to 30s for a lock instead of failing with a raw I/O error when
    # a host script (msc-sync.sh, migrations) is mid-write.
    cursor.execute("PRAGMA busy_timeout=30000")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.close()

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
