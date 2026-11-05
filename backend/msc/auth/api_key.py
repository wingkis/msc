"""API Key authentication and JWT support."""

import hashlib
import secrets
from datetime import datetime, timezone, timedelta
from typing import Optional

from jose import jwt, JWTError
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from sqlalchemy.orm import Session

from msc.database import get_db
from msc.models import ApiKey, PrefixRoute
from msc.config import get_settings

settings = get_settings()
# auto_error=False: with auth disabled there is no Authorization header at all,
# and even with auth enabled we want a clean 401 from our own code, not Starlette's 403.
security = HTTPBearer(auto_error=False)


_ALGORITHM = "HS256"
_TOKEN_EXPIRE_MINUTES = 1440  # 24 hours

# Identity used by every request while MSC_AUTH_ENABLED=false.
LAN_USER = {
    "user_id": 0,
    "username": "lan",
    "role": "admin",
    "prefix": None,
}


def hash_key(key: str) -> str:
    """Hash an API key with a random salt."""
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()
    return f"{salt}:{hashed}"


def verify_key(key: str, hashed: str) -> bool:
    """Verify an API key against its hash."""
    salt, stored_hash = hashed.split(":")
    computed = hashlib.sha256(f"{salt}:{key}".encode()).hexdigest()
    return computed == stored_hash


def generate_jwt(user_id: int, username: str, role: str, prefix: Optional[str] = None) -> str:
    """Generate a JWT token."""
    expire = datetime.now(timezone.utc) + timedelta(minutes=_TOKEN_EXPIRE_MINUTES)
    payload = {
        "sub": str(user_id),
        "username": username,
        "role": role,
        "prefix": prefix,
        "exp": expire,
    }
    return jwt.encode(payload, settings.secret_key, algorithm=_ALGORITHM)


def decode_jwt(token: str) -> dict:
    """Decode and verify a JWT token."""
    try:
        payload = jwt.decode(token, settings.secret_key, algorithms=[_ALGORITHM])
        return payload
    except JWTError:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")


async def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> dict:
    """Get current user from JWT token.

    When MSC_AUTH_ENABLED=false (default, LAN mode) this returns the anonymous
    admin identity without looking at the request at all.
    """
    if not settings.auth_enabled:
        return dict(LAN_USER)

    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Not authenticated")

    token = credentials.credentials
    payload = decode_jwt(token)
    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid token")

    return {
        "user_id": int(user_id),
        "username": payload.get("username", ""),
        "role": payload.get("role", "viewer"),
        "prefix": payload.get("prefix"),
    }


async def get_current_api_key(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Optional[dict]:
    """Get API key info if a valid key is provided. Returns None if no key or invalid key."""
    if credentials is None:
        return None

    raw_key = credentials.credentials
    if not raw_key.startswith("api-"):
        return None

    # Extract prefix: api-{prefix}-{rest}
    parts = raw_key.split("-", 2)
    if len(parts) < 3:
        return None
    prefix = parts[1]

    # Look up the API key in DB
    api_key_record = db.query(ApiKey).filter(
        ApiKey.key_prefix == prefix,
        ApiKey.is_active == 1,
    ).first()

    if not api_key_record:
        return None

    # Verify the full key
    if not verify_key(raw_key, api_key_record.key_hash):
        return None

    # Get route permissions (PrefixRoute has no is_active column)
    route = db.query(PrefixRoute).filter(
        PrefixRoute.prefix == prefix,
    ).first()

    # Update last_used
    api_key_record.last_used = datetime.now(timezone.utc).isoformat()
    db.commit()

    return {
        "key_id": api_key_record.id,
        "prefix": prefix,
        "description": api_key_record.description,
        "read_access": bool(route.read_access) if route else True,
        "write_access": bool(route.write_access) if route else True,
        "route_type": route.route_type if route else "full",
        "route_value": route.route_value if route else None,
    }


def auth_status() -> dict:
    """Auth mode, exposed to the Web UI via GET /api/v1/config."""
    return {
        "auth_enabled": settings.auth_enabled,
        "mode": "token" if settings.auth_enabled else "lan",
    }
