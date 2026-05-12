"""
Coupon Routes
- Admin: create, list, toggle, delete coupons
- Public: validate/apply a coupon code
"""

from datetime import datetime, timezone
from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.coupon import Coupon
from app.core.auth import require_admin
from app.schemas import CouponCreate, CouponRead, CouponApplyRequest, CouponApplyResponse

router = APIRouter(prefix="/coupons", tags=["Coupons"])


@router.post("", response_model=CouponRead, status_code=201)
def create_coupon(data: CouponCreate, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    existing = db.query(Coupon).filter(Coupon.code == data.code.upper().strip()).first()
    if existing:
        raise HTTPException(status_code=409, detail="Coupon code already exists")

    coupon = Coupon(**data.model_dump())
    coupon.code = coupon.code.upper().strip()
    db.add(coupon)
    db.commit()
    db.refresh(coupon)
    return coupon


@router.get("", response_model=list[CouponRead])
def list_coupons(active_only: bool = True, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    q = db.query(Coupon)
    if active_only:
        q = q.filter(Coupon.is_active == True)
    return q.order_by(Coupon.created_at.desc()).all()


@router.patch("/{coupon_id}/toggle", response_model=CouponRead)
def toggle_coupon(coupon_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    coupon.is_active = not coupon.is_active
    db.commit()
    db.refresh(coupon)
    return coupon


@router.delete("/{coupon_id}", status_code=204)
def delete_coupon(coupon_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")
    db.delete(coupon)
    db.commit()


@router.post("/validate", response_model=CouponApplyResponse)
def validate_coupon(data: CouponApplyRequest, db: Session = Depends(get_db)):
    """Public endpoint — check if a coupon is valid and preview discount."""
    coupon = db.query(Coupon).filter(
        Coupon.code == data.code.upper().strip(),
        Coupon.is_active == True,
    ).first()

    if not coupon:
        return CouponApplyResponse(valid=False, message="Invalid coupon code")

    if coupon.expires_at and coupon.expires_at < datetime.now(timezone.utc):
        return CouponApplyResponse(valid=False, message="Coupon has expired")

    if coupon.max_uses and coupon.used_count >= coupon.max_uses:
        return CouponApplyResponse(valid=False, message="Coupon usage limit reached")

    if data.order_total < coupon.min_order_value:
        return CouponApplyResponse(
            valid=False,
            message=f"Minimum order value is ₹{coupon.min_order_value}",
        )

    discount = coupon.calculate_discount(data.order_total)
    return CouponApplyResponse(
        valid=True,
        discount=discount,
        message=f"₹{discount} discount applied!",
    )
