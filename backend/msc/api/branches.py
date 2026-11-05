"""API routes: memory branches."""

from typing import List

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from msc.database import get_db
from msc.migrations import branch_counts
from msc.models import MemoryBranch, Memory
from msc.schemas import BranchCreate, MemoryResponse

router = APIRouter()


@router.get("")
def list_branches(db: Session = Depends(get_db)):
    """Every branch in use, plus the ones registered explicitly.

    Memories carry a free-text ``branch``; without this union a memory written
    with ``branch=main`` never showed up anywhere in the branch UI.
    """
    registered = {b.name: b for b in db.query(MemoryBranch).all()}
    counts = branch_counts()
    names = sorted(set(registered) | set(counts))
    return [{
        "id": registered[name].id if name in registered else None,
        "name": name,
        "parent_id": registered[name].parent_id if name in registered else None,
        "description": registered[name].description if name in registered else "",
        "memories": counts.get(name, 0),
        "registered": name in registered,
    } for name in names]


@router.post("", status_code=201)
def create_branch(data: BranchCreate, db: Session = Depends(get_db)):
    """Create a new branch."""
    name = (data.name or "").strip()
    if not name:
        raise HTTPException(status_code=422, detail="Branch name is required")

    existing = db.query(MemoryBranch).filter(MemoryBranch.name == name).first()
    if existing:
        raise HTTPException(status_code=409, detail=f"Branch already exists: {name}")

    branch = MemoryBranch(name=name, parent_id=data.parent_id, description=data.description)
    db.add(branch)
    db.commit()
    return {"id": branch.id, "name": branch.name, "parent_id": branch.parent_id,
            "description": branch.description}


@router.get("/{name}", response_model=List[MemoryResponse])
def get_branch_memories(name: str, db: Session = Depends(get_db)):
    """Get all memories in a branch."""
    memories = db.query(Memory).filter(
        Memory.branch == name,
        Memory.archived == 0,
    ).order_by(Memory.updated_at.desc()).all()

    if not memories and not db.query(MemoryBranch).filter(MemoryBranch.name == name).first():
        raise HTTPException(status_code=404, detail=f"Branch not found: {name}")

    return memories
