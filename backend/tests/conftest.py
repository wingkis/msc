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
    # RAG provider cards seed themselves from .env too — pin them empty so
    # the suite never embeds against a real endpoint. (MSC_RAG_ENABLED stays
    # on: the queue, degradation paths and the worker are under test.)
    "MSC_RAG_ENABLED": "true",
    "MSC_EMBED_BASE_URL": "", "MSC_EMBED_MODEL": "", "MSC_EMBED_API_KEY": "",
    "MSC_RERANK_BASE_URL": "", "MSC_RERANK_MODEL": "", "MSC_RERANK_API_KEY": "",
    "MSC_WHISPER_BASE_URL": "", "MSC_WHISPER_MODEL": "", "MSC_WHISPER_API_KEY": "",
    # Echo runs on its own card only when set; unset = falls back to chat.
    "MSC_ECHO_BASE_URL": "", "MSC_ECHO_MODEL": "", "MSC_ECHO_API_KEY": "",
    # Empty = echo md lands in the memory dir (the documented default).
    "MSC_ECHO_DIR": "",
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
