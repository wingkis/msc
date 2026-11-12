"""Application settings via pydantic-settings."""

from pathlib import Path
from typing import Optional

from pydantic_settings import BaseSettings


# Bumped together by the CLI, the Web UI and /api/v1/health so a mismatch is
# impossible to miss.
VERSION = "2.7.0"


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

    # Extra prefixes a file: ref may legitimately point at. The probe already
    # knows this service's own directories (database / backups / memory dirs /
    # photo roots); list more here — a source tree, a NAS mount — when the
    # container should be trusted to see them. A path outside every one of
    # these is reported "unchecked", never "gone".
    ref_roots: str = ""

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

    # Time decay — the automatic half of re-checking. A memory nobody has
    # verified or edited for this many days becomes "stale": still listed, still
    # recallable, but visibly unconfirmed instead of quietly forever active.
    # 0 disables both the worker and POST /memories/decay.
    stale_after_days: int = 90
    # How often the worker looks. The sweep is idempotent (only active rows
    # crossing the line get stamped), so a cheap hourly tick beats a nightly
    # batch that has to be remembered to be turned on.
    decay_interval_seconds: int = 3600

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

    # RAG — semantic search, voice capture, insights. Kill switch: with
    # MSC_RAG_ENABLED=false the service behaves exactly as it did in 2.2.0.
    # The cards below only seed the model_configs rows on first use; after
    # that the database wins, same doctrine as the chat card.
    rag_enabled: bool = True
    embed_base_url: str = ""
    embed_model: str = ""
    embed_api_key: str = ""
    rerank_base_url: str = ""
    rerank_model: str = ""
    rerank_api_key: str = ""
    whisper_base_url: str = ""
    whisper_model: str = ""
    whisper_api_key: str = ""
    # Echo (评注) can run on its own model card; unset it falls back to chat.
    echo_base_url: str = ""
    echo_model: str = ""
    echo_api_key: str = ""
    # Vision (照片描述) needs a multimodal endpoint; seed empty so the card
    # has to be pointed at one deliberately (text-only servers are common).
    vision_base_url: str = ""
    vision_model: str = ""
    vision_api_key: str = ""
    # Where /rag/echo's markdown lands when save=true. Empty = the memory
    # directory, so an echo becomes a first-class memory on the next sync.
    echo_dir: str = ""
    # Fusion tuning, overridable per key in the rag_settings table.
    rag_recall_top: int = 30
    rag_rerank_topn: int = 10
    rag_rrf_k: int = 60
    rag_embed_batch: int = 16

    # Photos (msc-pic): source cards, thumbnails, captioning. Kill switch:
    # MSC_PIC_ENABLED=false keeps the pic_worker off and answers /pic/* with 503.
    pic_enabled: bool = True
    # Thumbnails live under the data volume: thumbs/<source_id>/<item_id>.webp.
    pic_dir: Path = Path("/app/data/pics")
    # Local source cards may only point inside these roots (comma separated).
    # This is also the docker bind that makes photos visible to the container —
    # add a matching volume in docker-compose.yml when changing it.
    pic_roots: str = "/home/astro/photos"
    # Longest edge of the generated webp thumbnail.
    pic_thumb_px: int = 512
    # Cap for GET /pic/item/{id}/original streaming responses; 0 = uncapped.
    pic_max_original_mb: int = 256

    model_config = {"env_prefix": "MSC_", "env_file": ".env", "extra": "ignore"}


# Singleton
_settings: Optional[Settings] = None


def get_settings() -> Settings:
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
