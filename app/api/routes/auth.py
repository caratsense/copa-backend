"""
Auth Routes with SMS OTP MFA
==============================

REGISTRATION:
  POST /auth/register → customer account + JWT (no OTP on register)

LOGIN FLOW:
  Step 1: POST /auth/login (phone + password + device_fingerprint)
    → Staff: always returns requires_otp=true + temp_token
    → Customer on trusted device: returns access_token directly
    → Customer on new device: returns requires_otp=true + temp_token

  Step 2 (if OTP required): POST /auth/verify-otp (temp_token + otp)
    → Verifies OTP → returns real access_token
    → Saves device as trusted (for customers)

DEV MODE:
  When SMS_ENABLED=false, OTP is logged to console.
  Use "000000" as OTP to bypass in dev mode.

RATE LIMITS:
  /auth/login       → 5 per minute (prevent brute force)
  /auth/verify-otp  → 5 per minute
  /auth/resend-otp  → 3 per minute
  /auth/register    → 10 per minute
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.orm import Session
from slowapi import Limiter
from slowapi.util import get_remote_address

from app.db import get_db
from app.models.user import User, UserRole
from app.models.trusted_device import TrustedDevice
from app.core.auth import (
    hash_password, verify_password,
    create_access_token, decode_token,
    get_current_user,
)
from app.services.otp_service import send_otp, verify_otp
from app.schemas import (
    RegisterRequest, LoginRequest, LoginResponse,
    OTPVerifyRequest, TokenResponse, UserRead,
)

router = APIRouter(prefix="/auth", tags=["Auth"])
limiter = Limiter(key_func=get_remote_address)


# ─── REGISTER ─────────────────────────────────────────

@router.post("/register", response_model=TokenResponse, status_code=201)
@limiter.limit("10/minute")
def register(request: Request, data: RegisterRequest, db: Session = Depends(get_db)):
    """Register a new CUSTOMER account. No OTP on registration — direct token."""

    existing = db.query(User).filter(User.phone == data.phone).first()
    if existing:
        raise HTTPException(status_code=409, detail="Phone number already registered")

    user = User(
        name=data.name,
        phone=data.phone,
        email=data.email,
        date_of_birth=data.date_of_birth if data.date_of_birth else None,
        password_hash=hash_password(data.password),
        role=UserRole.CUSTOMER,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    token = create_access_token(user.id, user.role.value)
    return TokenResponse(
        access_token=token,
        user=UserRead.model_validate(user),
    )


# ─── LOGIN (STEP 1) ──────────────────────────────────

@router.post("/login", response_model=LoginResponse)
@limiter.limit("5/minute")
def login(request: Request, data: LoginRequest, db: Session = Depends(get_db)):
    """
    Step 1: Validate credentials.
    - Staff → always send OTP
    - Customer on trusted device → skip OTP, return JWT
    - Customer on new device → send OTP
    """

    user = db.query(User).filter(User.phone == data.phone).first()
    if not user:
        raise HTTPException(status_code=401, detail="Invalid phone or password")

    if not user.password_hash:
        raise HTTPException(status_code=401, detail="Account has no password. Please register.")

    if not verify_password(data.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid phone or password")

    if not user.is_active:
        raise HTTPException(status_code=403, detail="Account is deactivated")

    is_staff = user.role in (UserRole.ADMIN, UserRole.BAKER, UserRole.RIDER)

    # Check if customer has a trusted device
    if not is_staff and data.device_fingerprint:
        trusted = db.query(TrustedDevice).filter(
            TrustedDevice.user_id == user.id,
            TrustedDevice.device_fingerprint == data.device_fingerprint,
        ).first()

        if trusted:
            trusted.last_used_at = datetime.now(timezone.utc)
            db.commit()

            token = create_access_token(user.id, user.role.value)
            return LoginResponse(
                requires_otp=False,
                access_token=token,
                user=UserRead.model_validate(user),
                message="Welcome back! Logged in from trusted device.",
            )

    # OTP required — send it
    otp_result = send_otp(user.phone)

    # Create a short-lived temp token (10 min) for OTP verification only
    temp_token = create_access_token(user.id, "otp_pending", expires_minutes=10)

    return LoginResponse(
        requires_otp=True,
        temp_token=temp_token,
        message=otp_result.get("message", "OTP sent"),
    )


# ─── VERIFY OTP (STEP 2) ─────────────────────────────

@router.post("/verify-otp", response_model=TokenResponse)
@limiter.limit("5/minute")
def verify_otp_endpoint(request: Request, data: OTPVerifyRequest, db: Session = Depends(get_db)):
    """
    Step 2: Verify OTP → return real JWT.
    Saves device as trusted for customers.
    """

    payload = decode_token(data.temp_token)
    if payload.get("role") != "otp_pending":
        raise HTTPException(status_code=400, detail="Invalid token — use the temp_token from /login")

    user_id = int(payload["sub"])
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    if not verify_otp(user.phone, data.otp):
        raise HTTPException(status_code=401, detail="Invalid or expired OTP")

    # Save trusted device for customers
    is_staff = user.role in (UserRole.ADMIN, UserRole.BAKER, UserRole.RIDER)
    if not is_staff and data.device_fingerprint:
        existing = db.query(TrustedDevice).filter(
            TrustedDevice.user_id == user.id,
            TrustedDevice.device_fingerprint == data.device_fingerprint,
        ).first()

        if not existing:
            device = TrustedDevice(
                user_id=user.id,
                device_fingerprint=data.device_fingerprint,
            )
            db.add(device)
            db.commit()

    token = create_access_token(user.id, user.role.value)
    return TokenResponse(
        access_token=token,
        user=UserRead.model_validate(user),
    )


# ─── RESEND OTP ───────────────────────────────────────

@router.post("/resend-otp")
@limiter.limit("3/minute")
def resend_otp_endpoint(request: Request, data: OTPVerifyRequest, db: Session = Depends(get_db)):
    """Resend OTP if the user didn't receive it."""
    payload = decode_token(data.temp_token)
    if payload.get("role") != "otp_pending":
        raise HTTPException(status_code=400, detail="Invalid token")

    user_id = int(payload["sub"])
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        raise HTTPException(status_code=401, detail="User not found")

    result = send_otp(user.phone)
    return {"message": result.get("message", "OTP resent")}


# ─── PROFILE & DEVICES ───────────────────────────────

@router.get("/me", response_model=UserRead)
def get_me(user: User = Depends(get_current_user)):
    """Get the currently authenticated user's profile."""
    return user


@router.get("/my-devices", response_model=list[dict])
def my_devices(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """List trusted devices for the current user."""
    devices = db.query(TrustedDevice).filter(TrustedDevice.user_id == user.id).all()
    return [
        {
            "id": d.id,
            "device_name": d.device_name,
            "last_used_at": d.last_used_at.isoformat() if d.last_used_at else None,
            "created_at": d.created_at.isoformat() if d.created_at else None,
        }
        for d in devices
    ]


@router.delete("/devices/{device_id}", status_code=204)
def remove_device(device_id: int, user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Remove a trusted device (forces OTP on next login from that device)."""
    device = db.query(TrustedDevice).filter(
        TrustedDevice.id == device_id,
        TrustedDevice.user_id == user.id,
    ).first()
    if not device:
        raise HTTPException(status_code=404, detail="Device not found")
    db.delete(device)
    db.commit()


@router.delete("/devices", status_code=204)
def remove_all_devices(user: User = Depends(get_current_user), db: Session = Depends(get_db)):
    """Remove all trusted devices (forces OTP on all future logins)."""
    db.query(TrustedDevice).filter(TrustedDevice.user_id == user.id).delete()
    db.commit()
