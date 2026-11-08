"""Shared pytest setup.

Environment has to be in place before ``msc.*`` is imported, because the engine,
the settings singleton and the FTS bootstrap all happen at import time.
"""

import os
import sys
import tempfile
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

_TMP = Path(tempfile.mkdtemp(prefix="msc-test-"))
(_TMP / "memory").mkdir(parents=True, exist_ok=True)
(_TMP / "backups").mkdir(parents=True, exist_ok=True)

os.environ.update({
    "MSC_DB_PATH": str(_TMP / "msc.db"),
    "MSC_MEMORY_DIR": str(_TMP / "memory"),
    "MSC_CLAUDE_MEMORY_DIR": str(_TMP / "claude"),
    "MSC_BACKUP_DIR": str(_TMP / "backups"),
    "MSC_AUTH_ENABLED": "false",
    "MSC_READ_ONLY": "false",
    "MSC_ACCESS_LOG": "false",
    "MSC_SYNC_INTERVAL_SECONDS": "3600",
    # The model card seeds itself from .env, which belongs to whoever runs the
    # suite — pin it so the tests never inherit a real endpoint or schedule.
    "MSC_LLM_BASE_URL": "",
    "MSC_LLM_MODEL": "",
    "MSC_LLM_API_KEY": "",
    "MSC_SUMMARY_AUTO_ENABLED": "false",
    "MSC_SUMMARY_TIMEZONE": "",
    "MSC_SUMMARY_ARCHIVE_SOURCES": "false",
})


@pytest.fixture(scope="session")
def client():
    from fastapi.testclient import TestClient
    from msc.main import app

    with TestClient(app) as test_client:
        yield test_client


@pytest.fixture(scope="session")
def memory_dir():
    return _TMP / "memory"


@pytest.fixture(scope="session")
def backup_dir():
    return _TMP / "backups"


@pytest.fixture()
def db():
    from msc.database import SessionLocal

    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
