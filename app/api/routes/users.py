"""
User Routes — ADMIN ONLY.

Customer self-signup lives at POST /auth/register (which hashes a password).
Everything here is staff/back-office and requires an admin token.

/promote-admin is a break-glass bootstrap endpoint. It is DISABLED unless
PROMOTE_SECRET is explicitly set in the environment — there is no default.
"""

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.db import get_db
from app.models.user import User, UserRole
from app.schemas import UserCreate, UserRead
from app.core.auth import require_admin, get_current_user
from app.config import get_settings

router = APIRouter(prefix="/users", tags=["Users"])
settings = get_settings()
limiter = Limiter(key_func=get_remote_address)


def _normalize_phone(phone: str) -> str:
    p = phone.replace(" ", "").replace("-", "")
    if not p.startswith("+"):
        if p.startswith("91") and len(p) == 12:
            p = "+" + p
        elif len(p) == 10:
            p = "+91" + p
    return p


@router.post("", response_model=UserRead, status_code=201)
def create_user(
    data: UserCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin-only. Creates a passwordless user record (cannot log in until a
    password is set). For staff accounts prefer POST /admin/staff."""
    phone = _normalize_phone(data.phone)
    existing = db.query(User).filter(User.phone == phone).first()
    if existing:
        raise HTTPException(status_code=409, detail="Phone number already registered")

    payload = data.model_dump()
    payload["phone"] = phone
    user = User(**payload)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@router.get("/{user_id}", response_model=UserRead)
def get_user(
    user_id: int,
    requester: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Own profile, or any profile if admin."""
    if requester.id != user_id and requester.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="You can only view your own profile")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.get("", response_model=list[UserRead])
def list_users(
    skip: int = 0,
    limit: int = 50,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin-only — this returns customer names, phones and emails."""
    return db.query(User).offset(skip).limit(limit).all()


# ─── ADMIN BOOTSTRAP ─────────────────────────────────
# Disabled unless PROMOTE_SECRET is set. Secret goes in the body, never the
# query string (query strings land in access logs and browser history).

class PromoteRequest(BaseModel):
    phone: str
    secret: str


@router.post("/promote-admin", response_model=UserRead)
@limiter.limit("3/minute")
def promote_to_admin(request: Request, data: PromoteRequest, db: Session = Depends(get_db)):
    """Promote an existing user to admin. Requires PROMOTE_SECRET to be configured."""
    if not settings.PROMOTE_SECRET:
        raise HTTPException(
            status_code=404,
            detail="Not found",
        )
    # Constant-time compare so the secret can't be recovered by timing.
    import secrets as _secrets
    if not _secrets.compare_digest(data.secret, settings.PROMOTE_SECRET):
        raise HTTPException(status_code=403, detail="Invalid secret")

    user = db.query(User).filter(User.phone == _normalize_phone(data.phone)).first()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")

    user.role = UserRole.ADMIN
    db.commit()
    db.refresh(user)
    return user
