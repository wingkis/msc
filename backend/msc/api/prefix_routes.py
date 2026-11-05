"""API routes: prefix routes management."""


from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from msc.database import get_db
from msc.models import PrefixRoute
from msc.schemas import PrefixRouteCreate

router = APIRouter()


@router.get("")
def list_prefix_routes(db: Session = Depends(get_db)):
    """List all prefix routes."""
    routes = db.query(PrefixRoute).all()
    return [{"id": r.id, "prefix": r.prefix, "route_type": r.route_type,
             "route_value": r.route_value, "read_access": r.read_access,
             "write_access": r.write_access, "description": r.description}
            for r in routes]


@router.post("", status_code=201)
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


@router.delete("/{prefix}")
def delete_prefix_route(prefix: str, db: Session = Depends(get_db)):
    """Delete a prefix route."""
    route = db.query(PrefixRoute).filter(PrefixRoute.prefix == prefix).first()
    if not route:
        from fastapi import HTTPException
        raise HTTPException(status_code=404, detail="Route not found")

    db.delete(route)
    db.commit()
    return {"message": f"Route deleted: {prefix}"}
