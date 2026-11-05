"""API routes: API key management."""

import secrets
from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from msc.database import get_db
from msc.models import ApiKey, PrefixRoute
from msc.schemas import ApiKeyCreate, PrefixRouteCreate
from msc.auth.api_key import hash_key

router = APIRouter()


def _generate_api_key(prefix: str) -> str:
    """Generate a full API key: api-{prefix}-{random}"""
    random_part = secrets.token_hex(8)
    return f"api-{prefix}-{random_part}"


# ── API Key CRUD ──────────────────────────────────────────────────────

@router.get("")
def list_api_keys(db: Session = Depends(get_db)):
    """List all API keys."""
    return db.query(ApiKey).order_by(ApiKey.created_at.desc()).all()


@router.post("", status_code=201)
def create_api_key(data: ApiKeyCreate, db: Session = Depends(get_db)):
    """Create a new API key with specified prefix and route."""
    # Check if prefix route exists, create if not
    route = db.query(PrefixRoute).filter(PrefixRoute.prefix == data.prefix).first()
    if not route:
        route = PrefixRoute(
            prefix=data.prefix,
            route_type=data.route_type,
            route_value=data.route_value,
            read_access=1 if data.read_access else 0,
            write_access=1 if data.write_access else 0,
            description=data.description,
        )
        db.add(route)
        db.flush()

    # Generate API key
    raw_key = _generate_api_key(data.prefix)
    hashed = hash_key(raw_key)

    api_key = ApiKey(
        key_hash=hashed,
        key_prefix=data.prefix,
        description=data.description or f"API key for prefix {data.prefix}",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(api_key)
    db.commit()

    return {
        "id": api_key.id,
        "key_prefix": data.prefix,
        "api_key": raw_key,  # Only shown once!
        "description": data.description,
        "message": "Store this key securely — it cannot be shown again.",
    }


@router.put("/{key_id}")
def update_api_key(key_id: int, description: str, db: Session = Depends(get_db)):
    """Update API key description."""
    api_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    api_key.description = description
    db.commit()
    return api_key


@router.delete("/{key_id}")
def delete_api_key(key_id: int, db: Session = Depends(get_db)):
    """Disable (delete) an API key."""
    api_key = db.query(ApiKey).filter(ApiKey.id == key_id).first()
    if not api_key:
        raise HTTPException(status_code=404, detail="API key not found")

    api_key.is_active = 0
    db.commit()
    return {"message": "API key disabled"}


# ── Prefix Route CRUD ─────────────────────────────────────────────────

@router.get("/routes")
def list_prefix_routes(db: Session = Depends(get_db)):
    """List all prefix routes."""
    routes = db.query(PrefixRoute).all()
    return [{"id": r.id, "prefix": r.prefix, "route_type": r.route_type,
             "route_value": r.route_value, "read_access": r.read_access,
             "write_access": r.write_access, "description": r.description}
            for r in routes]


@router.post("/routes", status_code=201)
def create_prefix_route(data: PrefixRouteCreate, db: Session = Depends(get_db)):
    """Create or update a prefix route."""
    existing = db.query(PrefixRoute).filter(PrefixRoute.prefix == data.prefix).first()
    if existing:
        existing.route_type = data.route_type
        existing.route_value = data.route_value
        existing.read_access = 1 if data.read_access else 0
        existing.write_access = 1 if data.write_access else 0
        existing.description = data.description
    else:
        existing = PrefixRoute(
            prefix=data.prefix,
            route_type=data.route_type,
            route_value=data.route_value,
            read_access=1 if data.read_access else 0,
            write_access=1 if data.write_access else 0,
            description=data.description,
        )
        db.add(existing)
    db.commit()
    return existing


@router.delete("/routes/{prefix}")
def delete_prefix_route(prefix: str, db: Session = Depends(get_db)):
    """Delete a prefix route."""
    route = db.query(PrefixRoute).filter(PrefixRoute.prefix == prefix).first()
    if not route:
        raise HTTPException(status_code=404, detail="Route not found")

    db.delete(route)
    db.commit()
    return {"message": f"Route deleted: {prefix}"}
