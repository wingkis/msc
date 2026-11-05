"""RBAC decorators for API routes."""

from typing import Optional

from fastapi import Depends, HTTPException, status

from msc.auth.api_key import get_current_user


def require_write():
    """Check current user has write access."""
    async def check(user=Depends(get_current_user)):
        if user["role"] not in ("admin", "editor"):
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Write access denied")
    return check


def require_admin():
    """Check current user is admin."""
    async def check(user=Depends(get_current_user)):
        if user["role"] != "admin":
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admin access required")
    return check


async def check_api_key_scope(api_key_info: Optional[dict], need_write: bool = False) -> Optional[dict]:
    """Check API key has required scope for the operation.

    Returns the api_key_info or raises HTTPException.
    Returns None if no API key provided.
    """
    if api_key_info is None:
        return None

    # Full access: bypass all checks
    if api_key_info["route_type"] == "full":
        if not api_key_info["write_access"] and need_write:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key lacks write access")
        return api_key_info

    # Check read/write permissions
    if need_write and not api_key_info["write_access"]:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key lacks write access")
    if not api_key_info["read_access"] and not need_write:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="API key lacks read access")

    return api_key_info
