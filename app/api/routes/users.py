import os
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User, UserRole
from app.schemas import UserCreate, UserRead
from app.core.auth import hash_password

router = APIRouter(prefix="/users", tags=["Users"])

PROMOTE_SECRET = os.getenv("PROMOTE_SECRET", "cakeoclock_promote_2026")


@router.post("", response_model=UserRead, status_code=201)
def create_user(data: UserCreate, db: Session = Depends(get_db)):
    existing = db.query(User).filter(User.phone == data.phone).first()
    if existing:
        raise HTTPException(status_code=409, detail="Phone number already registered")
    user = User(**data.model_dump())
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserRead)
def get_user(user_id: int, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("", response_model=list[UserRead])
def list_users(skip: int = 0, limit: int = 50, db: Session = Depends(get_db)):
    return db.query(User).offset(skip).limit(limit).all()


@router.post("/promote-admin", response_model=UserRead)
def promote_to_admin(phone: str, secret: str, new_password: str | None = None, db: Session = Depends(get_db)):
    """One-time endpoint to promote a user to admin. Requires secret key."""
    if secret != PROMOTE_SECRET:
        raise HTTPException(status_code=403, detail="Invalid secret")

    # Normalise phone
    p = phone.replace(" ", "").replace("-", "")
    if not p.startswith("+"):
        p = "+91" + p if len(p) == 10 else p

    user = db.query(User).filter(User.phone == p).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.role = UserRole.ADMIN
    if new_password:
        user.password_hash = hash_password(new_password)
    db.commit()
    db.refresh(user)
    return user
