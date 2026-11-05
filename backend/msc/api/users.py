"""API routes: user management (admin only)."""

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from msc.database import get_db
from msc.models import User
from msc.auth.api_key import hash_key, generate_jwt

router = APIRouter()

ROLES = ("admin", "editor", "viewer")


def _check_role(role: str) -> str:
    """The users table enforces this with a CHECK constraint; unchecked input
    surfaces as a 500 IntegrityError instead of a usable 422."""
    if role not in ROLES:
        raise HTTPException(status_code=422, detail=f"Invalid role {role!r}; allowed: {', '.join(ROLES)}")
    return role


@router.get("")
def list_users(db: Session = Depends(get_db)):
    """List all users (admin only)."""
    users = db.query(User).filter(User.is_active == 1).all()
    return [{"id": u.id, "username": u.username, "display_name": u.display_name,
             "role": u.role, "is_active": u.is_active} for u in users]


@router.post("", status_code=201)
def create_user(
    username: str,
    password: str,
    role: str = "editor",
    display_name: str = "",
    db: Session = Depends(get_db),
):
    """Create a new user (admin only)."""
    _check_role(role)
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        raise HTTPException(status_code=409, detail="Username already exists")

    user = User(
        username=username,
        display_name=display_name or username,
        role=role,
        api_key_hash=hash_key(password),
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    db.add(user)
    db.commit()

    token = generate_jwt(user.id, user.username, user.role)
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "token": token,
        "message": "User created. Store this token securely.",
    }


@router.put("/{user_id}")
def update_user(user_id: int, role: str = None, display_name: str = None, db: Session = Depends(get_db)):
    """Update a user (admin only)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    if role:
        _check_role(role)
        user.role = role
    if display_name:
        user.display_name = display_name
    db.commit()
    return user


@router.delete("/{user_id}")
def deactivate_user(user_id: int, db: Session = Depends(get_db)):
    """Deactivate a user (admin only)."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.is_active = 0
    db.commit()
    return {"message": f"User deactivated: {user.username}"}
