"""Party extras — balloons, candles, gift wrapping, etc."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.extra import Extra
from app.models.user import User
from app.core.auth import require_admin
from app.schemas import ExtraCreate, ExtraRead

router = APIRouter(prefix="/extras", tags=["Extras"])


@router.get("", response_model=list[ExtraRead])
def list_extras(db: Session = Depends(get_db)):
    """Public — list active extras."""
    return db.query(Extra).filter(Extra.is_active == True).order_by(Extra.sort_order.asc()).all()


@router.get("/all", response_model=list[ExtraRead])
def list_all_extras(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin — list all extras including inactive."""
    return db.query(Extra).order_by(Extra.sort_order.asc()).all()


@router.post("", response_model=ExtraRead)
def create_extra(data: ExtraCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    extra = Extra(name=data.name, description=data.description, price=data.price)
    db.add(extra)
    db.commit()
    db.refresh(extra)
    return extra


@router.patch("/{extra_id}", response_model=ExtraRead)
def update_extra(extra_id: int, data: ExtraCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    extra = db.query(Extra).filter(Extra.id == extra_id).first()
    if not extra:
        raise HTTPException(status_code=404, detail="Extra not found")
    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(extra, key, val)
    db.commit()
    db.refresh(extra)
    return extra


@router.patch("/{extra_id}/toggle")
def toggle_extra(extra_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    extra = db.query(Extra).filter(Extra.id == extra_id).first()
    if not extra:
        raise HTTPException(status_code=404, detail="Extra not found")
    extra.is_active = not extra.is_active
    db.commit()
    return {"id": extra.id, "is_active": extra.is_active}


@router.delete("/{extra_id}")
def delete_extra(extra_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    extra = db.query(Extra).filter(Extra.id == extra_id).first()
    if not extra:
        raise HTTPException(status_code=404, detail="Extra not found")
    db.delete(extra)
    db.commit()
    return {"detail": "Extra deleted"}
