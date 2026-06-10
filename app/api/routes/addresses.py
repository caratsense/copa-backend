"""Customer saved addresses — CRUD endpoints."""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.address import Address
from app.models.user import User
from app.core.auth import get_current_user
from app.schemas import AddressCreate, AddressRead

router = APIRouter(prefix="/addresses", tags=["Addresses"])


@router.get("", response_model=list[AddressRead])
def list_addresses(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    return db.query(Address).filter(Address.user_id == user.id).order_by(Address.is_default.desc(), Address.created_at.desc()).all()


@router.post("", response_model=AddressRead)
def create_address(data: AddressCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    # If setting as default, unset others
    if data.is_default:
        db.query(Address).filter(Address.user_id == user.id).update({"is_default": False})

    addr = Address(
        user_id=user.id,
        label=data.label,
        full_address=data.full_address,
        flat_building=data.flat_building,
        landmark=data.landmark,
        latitude=data.latitude,
        longitude=data.longitude,
        is_default=data.is_default,
    )
    db.add(addr)
    db.commit()
    db.refresh(addr)
    return addr


@router.patch("/{address_id}", response_model=AddressRead)
def update_address(address_id: int, data: AddressCreate, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    addr = db.query(Address).filter(Address.id == address_id, Address.user_id == user.id).first()
    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")

    if data.is_default:
        db.query(Address).filter(Address.user_id == user.id).update({"is_default": False})

    for key, val in data.model_dump(exclude_unset=True).items():
        setattr(addr, key, val)
    db.commit()
    db.refresh(addr)
    return addr


@router.delete("/{address_id}")
def delete_address(address_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    addr = db.query(Address).filter(Address.id == address_id, Address.user_id == user.id).first()
    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")
    db.delete(addr)
    db.commit()
    return {"detail": "Address deleted"}


@router.patch("/{address_id}/set-default")
def set_default(address_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    addr = db.query(Address).filter(Address.id == address_id, Address.user_id == user.id).first()
    if not addr:
        raise HTTPException(status_code=404, detail="Address not found")
    db.query(Address).filter(Address.user_id == user.id).update({"is_default": False})
    addr.is_default = True
    db.commit()
    return {"detail": f"'{addr.label}' set as default"}
