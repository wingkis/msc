"""FastAPI app factory."""

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, Depends
from starlette.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy.orm import Session

from msc.config import VERSION, get_settings
from msc.api import memories, search, versions, sync, backup, auth, api_keys, prefix_routes, branches, users, meta
from msc.api import summarize, rag
from msc.filesystem.indexer import build_index
from msc.models import Memory, User, ApiKey, PrefixRoute
from msc.database import get_db
from msc.migrations import health_detail, run_maintenance

settings = get_settings()

# create_all only creates missing tables, so an upgrade that adds a column never
# reached an existing database. run_maintenance() closes that gap and repairs the
# leftovers from the old importer (malformed tags, stale search index).
_maintenance = run_maintenance()
if any(_maintenance[k] for k in
       ("columns_added", "tags_normalized", "branches_registered", "bodies_cleaned",
        "model_purpose_backfilled", "embeddings_pruned")
       ) or _maintenance["search_index"]["missing"]:
    print(f"Startup maintenance: {_maintenance}")

app = FastAPI(
    title="MSC Memory",
    description="Memory Sync Client — Personal memory management service",
    version=VERSION,
)

# CORS
# MSC_CORS_ORIGINS accepts "a,b" as well as the JSON-ish '["a", "b"]' the
# example .env uses; taking the value literally produced an origin named
# '["*"]', which matches nothing.
cors_raw = settings.cors_origins.strip()
if cors_raw.startswith("["):
    try:
        cors_origins = [str(o).strip() for o in json.loads(cors_raw) if str(o).strip()]
    except json.JSONDecodeError:
        cors_origins = []
else:
    cors_origins = [o.strip() for o in cors_raw.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=cors_origins,
    # Browsers reject Access-Control-Allow-Credentials next to a wildcard
    # origin, which silently breaks cross-origin calls to this API.
    allow_credentials="*" not in cors_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

# API Key middleware — parse X-API-Key header and attach to request.state
from starlette.middleware.base import BaseHTTPMiddleware

class ApiKeyMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        from msc.database import SessionLocal
        from msc.auth.api_key import verify_key

        # LAN mode: no key lookup, no last_used write — every request is open.
        if not settings.auth_enabled:
            return await call_next(request)

        api_key = request.headers.get("X-API-Key", "")
        if api_key.startswith("api-"):
            parts = api_key.split("-", 2)
            if len(parts) >= 3:
                prefix = parts[1]
                db = SessionLocal()
                try:
                    key_record = db.query(ApiKey).filter(
                        ApiKey.key_prefix == prefix,
                        ApiKey.is_active == 1,
                    ).first()
                    if key_record and verify_key(api_key, key_record.key_hash):
                        route = db.query(PrefixRoute).filter(
                            PrefixRoute.prefix == prefix,
                        ).first()
                        request.state.api_key_info = {
                            "key_id": key_record.id,
                            "prefix": prefix,
                            "description": key_record.description,
                            "read_access": bool(route.read_access) if route else True,
                            "write_access": bool(route.write_access) if route else True,
                            "route_type": route.route_type if route else "full",
                            "route_value": route.route_value if route else None,
                        }
                        key_record.last_used = __import__("datetime").datetime.now(__import__("datetime").timezone.utc).isoformat()
                        db.commit()
                finally:
                    db.close()
        response = await call_next(request)
        return response

app.add_middleware(ApiKeyMiddleware)


# Audit trail. A service with no login has no users to blame, so every write
# logs its self-reported actor and source address instead.
import logging
import time

_logger = logging.getLogger("msc.access")


class AccessLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        started = time.perf_counter()
        response = await call_next(request)
        if settings.access_log and request.url.path.startswith("/api/"):
            actor = request.headers.get("X-Agent") or request.headers.get("X-Client") or "-"
            client = request.headers.get("X-Forwarded-For") or (
                request.client.host if request.client else "-")
            _logger.info(
                '%s %s -> %s %.0fms actor=%s ip=%s',
                request.method, request.url.path, response.status_code,
                (time.perf_counter() - started) * 1000, actor, client,
            )
        return response


app.add_middleware(AccessLogMiddleware)

# Web UI (served at / and /ui)
ui_dir = Path(__file__).parent.parent / "ui"
if ui_dir.exists():
    app.mount("/ui", StaticFiles(directory=str(ui_dir), html=True), name="ui")

# Serve index.html at root
@app.get("/")
async def serve_index():
    return FileResponse(str(ui_dir / "index.html"))

# Health
@app.get("/api/v1/health")
def health():
    from msc.auth.api_key import auth_status

    detail = health_detail() if settings.health_detail else {}
    return {"status": "ok", "version": VERSION, **auth_status(), **detail}

# Client config — tells the Web UI/CLI whether login is needed
@app.get("/api/v1/config")
def client_config():
    from msc.auth.api_key import auth_status
    return {**auth_status(), "version": VERSION, "read_only": settings.read_only}

# Stats
@app.get("/api/v1/stats")
def stats(db: Session = Depends(get_db)):
    from sqlalchemy import func
    total = db.query(Memory).filter(Memory.archived == 0).count()
    by_category = {r[0]: r[1] for r in db.query(Memory.category, func.count("*")).filter(Memory.archived == 0).group_by(Memory.category).all()}
    by_priority = {r[0]: r[1] for r in db.query(Memory.priority, func.count("*")).filter(Memory.archived == 0).group_by(Memory.priority).all()}
    by_source = {r[0]: r[1] for r in db.query(Memory.source, func.count("*")).filter(Memory.archived == 0).group_by(Memory.source).all()}
    return {
        "total": total,
        "by_category": by_category,
        "by_priority": by_priority,
        "by_source": by_source,
    }


# Register routers
app.include_router(auth.router, prefix="/api/v1/auth", tags=["auth"])
app.include_router(memories.router, prefix="/api/v1/memories", tags=["memories"])
app.include_router(search.router, prefix="/api/v1/search", tags=["search"])
app.include_router(versions.router, prefix="/api/v1/memories", tags=["versions"])
app.include_router(sync.router, prefix="/api/v1/sync", tags=["sync"])
app.include_router(backup.router, prefix="/api/v1/backup", tags=["backup"])
app.include_router(api_keys.router, prefix="/api/v1/api-keys", tags=["api-keys"])
app.include_router(prefix_routes.router, prefix="/api/v1/prefix-routes", tags=["prefix-routes"])
app.include_router(branches.router, prefix="/api/v1/branches", tags=["branches"])
app.include_router(users.router, prefix="/api/v1/users", tags=["users"])
app.include_router(meta.router, prefix="/api/v1", tags=["meta"])
# Model card + digests: Web UI only, so they stay out of /help and /llms.txt.
app.include_router(summarize.router, prefix="/api/v1", tags=["summarize"])
# RAG (semantic search index, providers, voice, insights): same Web-UI-only rule.
app.include_router(rag.router, prefix="/api/v1/rag", tags=["rag"])


# Agent-facing entry points: curl-able plain text, no HTML.
@app.get("/llms.txt", include_in_schema=False)
async def llms_txt():
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(meta.help_body(), media_type="text/plain; charset=utf-8")


@app.post("/api/v1/setup")
def setup(data: dict = None):
    """One-time admin setup. Creates first admin user and API key."""
    from msc.database import SessionLocal
    from msc.auth.api_key import hash_key
    import secrets

    db = SessionLocal()

    # Only allow setup if no users exist
    if db.query(User).count() > 0:
        db.close()
        return {"message": "Admin already configured. Use /api/v1/auth/register for new users."}

    # Build JSON index if not exists
    index_path = settings.memory_dir / ".msc_index.json"
    if not index_path.exists():
        build_index()

    # Generate admin credentials
    username = "admin"
    password = secrets.token_urlsafe(12)
    prefix = "999"

    user = User(
        username=username,
        api_key_hash=hash_key(password),
        role="admin",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(user)
    db.flush()

    api_key = ApiKey(
        key_hash=hash_key(f"api-{prefix}-{password}"),
        key_prefix=prefix,
        description="Initial admin key",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(api_key)

    prefix_route = PrefixRoute(
        prefix=prefix,
        route_type="full",
        read_access=1,
        write_access=1,
        description="Full admin access",
    )
    db.add(prefix_route)
    db.commit()

    db.close()
    return {
        "message": "Admin user created",
        "username": username,
        "api_key": f"api-{prefix}-{password}",
        "warning": "Save this API key — it will not be shown again!"
    }


# ── Start server ────────────────────────────────────────────────────

if __name__ == "__main__":
    from msc.workers import start_workers
    start_workers()
    import uvicorn
    uvicorn.run(
        "msc.main:app",
        host=settings.bind_host,
        port=settings.bind_port,
        log_level="info",
    )
