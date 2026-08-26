"""
Authentication & Authorization System
======================================
- JWT-based login (phone + password)
- Password hashing with bcrypt
- Role-based access control
- FastAPI dependencies for route protection

FLOW:
1. POST /auth/register → creates user with hashed password
2. POST /auth/login    → returns JWT token
3. All protected routes use Authorization: Bearer <token>
4. Admin routes additionally check role == admin
"""

from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from jose import jwt, JWTError
import bcrypt
from sqlalchemy.orm import Session

from app.config import get_settings
from app.db import get_db
from app.models.user import User, UserRole

settings = get_settings()

# Role claim on the short-lived token handed out between password/phone entry
# and OTP verification. It is not a real role and must never authorise anything
# except /auth/verify-otp.
OTP_PENDING_ROLE = "otp_pending"

# ─── PASSWORD HASHING ─────────────────────────────────


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(plain: str, hashed: str) -> bool:
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


# ─── JWT TOKENS ───────────────────────────────────────

def create_access_token(user_id: int, role: str, expires_minutes: int | None = None) -> str:
    expire = datetime.now(timezone.utc) + timedelta(
        minutes=expires_minutes or settings.JWT_EXPIRY_MINUTES
    )
    payload = {
        "sub": str(user_id),
        "role": role,
        "exp": expire,
    }
    return jwt.encode(payload, settings.JWT_SECRET, algorithm=settings.JWT_ALGORITHM)


def decode_token(token: str) -> dict:
    try:
        return jwt.decode(token, settings.JWT_SECRET, algorithms=[settings.JWT_ALGORITHM])
    except JWTError:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or expired token",
        )


# ─── FASTAPI DEPENDENCIES ────────────────────────────

security = HTTPBearer(auto_error=False)


def get_current_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> User:
    """Extract and validate user from Bearer token."""
    if credentials is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated — send Authorization: Bearer <token>",
        )

    payload = decode_token(credentials.credentials)

    # The half-authenticated token issued by /auth/login and /auth/login-otp
    # carries role="otp_pending" and is ONLY valid at /auth/verify-otp. It used
    # to authenticate every endpoint, which made /auth/login-otp — a passwordless
    # endpoint that takes nothing but a phone number — a complete account
    # takeover: request a temp_token for any phone, use it as a bearer token,
    # never supply the OTP.
    if payload.get("role") == OTP_PENDING_ROLE:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="OTP verification required — complete login at /auth/verify-otp",
        )

    try:
        user_id = int(payload["sub"])
    except (KeyError, TypeError, ValueError):
        raise HTTPException(status_code=401, detail="Malformed token")

    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")
    # Deactivation must take effect immediately, not whenever the token happens
    # to expire — otherwise an offboarded staff member keeps full API access
    # (including the admin delivery feed's customer names, phones and
    # addresses) for the rest of the 24h token lifetime.
    if not user.is_active:
        raise HTTPException(status_code=401, detail="Account is deactivated")
    return user


def get_optional_user(
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
    db: Session = Depends(get_db),
) -> Optional[User]:
    """Like get_current_user but returns None instead of 401 — for public endpoints."""
    if credentials is None:
        return None
    try:
        return get_current_user(credentials, db)
    except HTTPException:
        return None


def can_observe_delivery(user: User, order) -> bool:
    """
    May this user see where an order's rider physically is?

    - admin    → any order
    - customer → only orders they placed
    - rider    → only orders assigned to them
    - baker    → no; a baker has no reason to see a customer's live position
                 or their home coordinates

    Shared by the REST location endpoint and the tracking WebSocket so the two
    cannot drift apart — they expose the same data and must answer this the
    same way.
    """
    if user.role == UserRole.ADMIN:
        return True
    if user.role == UserRole.CUSTOMER:
        return order.user_id == user.id
    if user.role == UserRole.RIDER:
        return order.assigned_rider_id == user.id
    return False


def require_admin(user: User = Depends(get_current_user)) -> User:
    """Only allows users with role=admin."""
    if user.role != UserRole.ADMIN:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Admin access required. Your role: {user.role.value}",
        )
    return user


def require_role(*roles: UserRole):
    """
    Factory — create a dependency that allows specific roles.

    Usage:
        @router.get("/baker/queue")
        def baker_queue(user: User = Depends(require_role(UserRole.BAKER, UserRole.ADMIN))):
            ...
    """
    def dependency(user: User = Depends(get_current_user)) -> User:
        if user.role not in roles:
            allowed = ", ".join(r.value for r in roles)
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=f"Access denied. Allowed: {allowed}. Your role: {user.role.value}",
            )
        return user
    return dependency
