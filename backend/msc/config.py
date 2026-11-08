"""Application settings via pydantic-settings."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


# Bumped together by the CLI, the Web UI and /api/v1/health so a mismatch is
# impossible to miss.
VERSION = "2.2.0"


class Settings(BaseSettings):
    # Database
    db_path: Path = Path("/app/data/msc.db")

    # Memory filesystem paths
    memory_dir: Path = Path("/home/astro/.msc/memory")
    claude_memory_dir: Path = Path("/home/astro/.claude/projects/-home-astro/memory")

    # Backup
    backup_dir: Path = Path("/app/backups")
    # How many archives the scheduled backup keeps. 0 disables pruning.
    backup_keep: int = 14

    # Auth
    # auth_enabled=False → 局域网免登录模式：所有 API 与 Web UI 无需 token / API Key
    auth_enabled: bool = False
    secret_key: str = ""
    initial_admin_key: Optional[str] = None

    # Server
    bind_host: str = "0.0.0.0"
    bind_port: int = 8000
    cors_origins: str = "*"
    # Emergency switch: serve reads, reject every write.
    read_only: bool = False
    # One line per request in the container log — the audit trail of a no-login service.
    access_log: bool = True
    # Externally reachable address, used in /api/v1/help so agents get a URL
    # that actually works from their own host.
    public_url: str = ""
    # Report DB/search-index self-checks in /api/v1/health.
    health_detail: bool = True

    # Sync
    sync_interval_seconds: int = 60

    # Model card — first-run defaults only. The Web UI writes the real values
    # into the model_configs table, so switching models never needs a rebuild.
    llm_base_url: str = ""
    llm_model: str = ""
    llm_api_key: str = ""
    llm_temperature: float = 0.2
    llm_max_tokens: int = 1500
    llm_timeout_seconds: int = 180
    # Small local models have short context windows; the prompt is trimmed to this.
    llm_max_input_chars: int = 12000

    # Daily digest — the automatic half of the model card.
    summary_auto_enabled: bool = False
    summary_auto_time: str = "23:30"
    # Empty = the container's own timezone, which is UTC in the stock image.
    summary_timezone: str = ""
    summary_category: str = "project"
    summary_priority: str = "medium"
    # Whether the memories a digest was built from get soft-deleted afterwards.
    summary_archive_sources: bool = False

    model_config = {"env_prefix": "MSC_", "env_file": ".env", "extra": "ignore"}


# Singleton
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
