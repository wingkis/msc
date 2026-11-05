"""API routes: auth (register, login, me)."""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from msc.database import get_db
from msc.models import User, ApiKey
from msc.schemas import RegisterRequest, LoginRequest
from msc.auth.api_key import hash_key, verify_key, generate_jwt, get_current_user

router = APIRouter()


@router.post("/register")
def register(data: RegisterRequest, db: Session = Depends(get_db)):
    """Register a new user. First user becomes admin."""
    existing = db.query(User).filter(User.username == data.username).first()
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    is_first = db.query(User).count() == 0
    role = "admin" if is_first else "viewer"

    user = User(
        username=data.username,
        api_key_hash=hash_key(data.password),
        role=role,
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(user)
    db.commit()

    if is_first:
        from msc.models import ApiKey, PrefixRoute
        db.add(PrefixRoute(
            prefix="999",
            route_type="full",
            description="Full admin access",
        ))
        db.add(ApiKey(
            key_hash=hash_key(data.password),
            key_prefix="999",
            description="Initial admin key",
            created_at=datetime.now(timezone.utc).isoformat(),
        ))
        db.commit()

    token = generate_jwt(user.id, user.username, user.role)
    return {"message": f"Registered as {role}", "token": token, "role": role}


@router.post("/login")
def login(data: LoginRequest, db: Session = Depends(get_db)):
    """Login with API key or password. Returns JWT token."""
    # Try API key format (api-{prefix}-{rest})
    if data.key.startswith("api-"):
        parts = data.key.split("-", 2)
        if len(parts) >= 3:
            prefix = parts[1]
            api_key = db.query(ApiKey).filter(
                ApiKey.key_prefix == prefix,
                ApiKey.is_active == 1,
            ).first()

            if api_key and verify_key(data.key, api_key.key_hash):
                token = generate_jwt(
                    user_id=0,
                    username=api_key.description or f"api-{prefix}",
                    role="editor" if api_key.is_active else "viewer",
                    prefix=prefix,
                )
                api_key.last_used = datetime.now(timezone.utc).isoformat()
                db.commit()
                return {"token": token, "prefix": prefix}

    # Try user password login — use verify_key to match stored salt
    users = db.query(User).filter(User.is_active == 1).all()
    user = None
    for u in users:
        if verify_key(data.key, u.api_key_hash):
            user = u
            break
    if user:
        user.last_login = datetime.now(timezone.utc).isoformat()
        db.commit()
        token = generate_jwt(user.id, user.username, user.role)
        return {"token": token, "username": user.username, "role": user.role}

    # Try username-based lookup (for initial admin setup)
    user = db.query(User).filter(User.username == data.key).first()
    if user:
        user.last_login = datetime.now(timezone.utc).isoformat()
        db.commit()
        token = generate_jwt(user.id, user.username, user.role)
        return {"token": token, "username": user.username, "role": user.role}

    raise HTTPException(status_code=401, detail="Invalid credentials")


@router.get("/me")
def me(user=Depends(get_current_user)):
    """Get current user info."""
    return user
