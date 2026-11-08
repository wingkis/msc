"""Read/write the single model-card row.

``.env`` only seeds the first row — after that the database is the source of
truth, so a user on another machine can point MSC at their own model from the
Web UI without touching a config file or rebuilding the image.
"""

import re
from datetime import datetime, timezone

from msc.config import get_settings
from msc.models import ModelConfig

settings = get_settings()

AUTO_TIME_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")
CATEGORIES = ("config", "project", "user", "knowledge", "reference")
PRIORITIES = ("low", "medium", "high", "critical")

# Columns the API may write. api_key is handled separately: an omitted key keeps
# the stored one, an empty string clears it.
EDITABLE = (
    "label", "base_url", "model", "enabled", "temperature", "max_tokens",
    "timeout_seconds", "max_input_chars", "extra_json", "prompt_template",
    "auto_enabled", "auto_time", "auto_timezone", "default_category",
    "default_priority", "archive_sources",
)
BOOL_FIELDS = ("enabled", "auto_enabled", "archive_sources")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_config(db) -> ModelConfig:
    """The card, created from the environment on first use."""
    row = db.query(ModelConfig).filter(ModelConfig.id == 1).first()
    if row is not None:
        return row

    row = ModelConfig(
        id=1,
        label="本地模型",
        base_url=settings.llm_base_url,
        model=settings.llm_model,
        api_key=settings.llm_api_key,
        enabled=1,
        temperature=settings.llm_temperature,
        max_tokens=settings.llm_max_tokens,
        timeout_seconds=settings.llm_timeout_seconds,
        max_input_chars=settings.llm_max_input_chars,
        auto_enabled=1 if settings.summary_auto_enabled else 0,
        auto_time=settings.summary_auto_time,
        auto_timezone=settings.summary_timezone,
        default_category=settings.summary_category,
        default_priority=settings.summary_priority,
        archive_sources=1 if settings.summary_archive_sources else 0,
        updated_at=_now(),
    )
    db.add(row)
    db.commit()
    return row


def update_config(db, patch: dict) -> ModelConfig:
    """Apply a partial update, rejecting values the rest of the app cannot use.

    Raises ValueError so the API can turn it into a 422 that names the problem.
    """
    row = get_config(db)

    if "api_key" in patch:
        row.api_key = (patch.get("api_key") or "").strip()

    for field in EDITABLE:
        if field not in patch or patch[field] is None:
            continue
        value = patch[field]

        if field in BOOL_FIELDS:
            value = 1 if value else 0
        elif field == "auto_time":
            value = str(value).strip()
            if not AUTO_TIME_RE.match(value):
                raise ValueError(f"auto_time 需要 HH:MM 格式，收到 {value!r}")
        elif field == "default_category":
            if value not in CATEGORIES:
                raise ValueError(f"Invalid default_category {value!r}; allowed: {', '.join(CATEGORIES)}")
        elif field == "default_priority":
            if value not in PRIORITIES:
                raise ValueError(f"Invalid default_priority {value!r}; allowed: {', '.join(PRIORITIES)}")
        elif field in ("max_tokens", "timeout_seconds", "max_input_chars"):
            value = int(value)
            if value <= 0:
                raise ValueError(f"{field} 必须是正整数")
        elif field == "temperature":
            value = float(value)
            if not 0.0 <= value <= 2.0:
                raise ValueError("temperature 需要在 0 到 2 之间")
        elif field == "base_url":
            value = str(value).strip()
            if value and "://" not in value and not value.startswith("localhost"):
                value = "http://" + value

        setattr(row, field, value)

    if row.model and not row.base_url:
        raise ValueError("填了模型名就必须填 API 地址")

    row.updated_at = _now()
    db.commit()
    return row


def mask_key(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}…{api_key[-4:]}"


def public_view(row: ModelConfig) -> dict:
    """The card as the UI sees it: the API key is never sent back in full."""
    from msc.services import llm

    try:
        endpoint = llm.chat_endpoint(row.base_url)
    except llm.LLMError as exc:
        endpoint = str(exc)

    return {
        "id": row.id,
        "label": row.label or "",
        "base_url": row.base_url or "",
        "model": row.model or "",
        "api_key_set": bool(row.api_key),
        "api_key_masked": mask_key(row.api_key or ""),
        "enabled": bool(row.enabled),
        "temperature": row.temperature,
        "max_tokens": row.max_tokens,
        "timeout_seconds": row.timeout_seconds,
        "max_input_chars": row.max_input_chars,
        "extra_json": row.extra_json or "",
        "prompt_template": row.prompt_template or "",
        "auto_enabled": bool(row.auto_enabled),
        "auto_time": row.auto_time or "",
        "auto_timezone": row.auto_timezone or "",
        "default_category": row.default_category or "project",
        "default_priority": row.default_priority or "medium",
        "archive_sources": bool(row.archive_sources),
        "endpoint": endpoint,
        "ready": bool(row.base_url and row.model and row.enabled),
        "last_ok_at": row.last_ok_at,
        "last_error": row.last_error,
        "updated_at": row.updated_at,
    }


def note_result(db, row: ModelConfig, error: str = "") -> None:
    """Record the outcome of the last model call, for the card's status line."""
    if error:
        row.last_error = str(error)[:500]
    else:
        row.last_error = None
        row.last_ok_at = _now()
    row.updated_at = _now()
    db.commit()
