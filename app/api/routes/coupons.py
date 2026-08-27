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
from app.schemas import CouponCreate, CouponUpdate, CouponRead, CouponApplyRequest, CouponApplyResponse

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


@router.patch("/{coupon_id}", response_model=CouponRead)
def update_coupon(
    coupon_id: int,
    data: CouponUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Edit a coupon without resetting its usage.

    There was no update route, so fixing a wrong discount or adding a missing
    expiry meant deleting and recreating - which resets used_count to zero and
    re-opens a max_uses cap that had already been spent, letting a code that
    was meant to run out be redeemed all over again.
    """
    coupon = db.query(Coupon).filter(Coupon.id == coupon_id).first()
    if not coupon:
        raise HTTPException(status_code=404, detail="Coupon not found")

    changes = data.model_dump(exclude_unset=True)
    if "discount_type" in changes and changes["discount_type"] not in ("flat", "percentage"):
        raise HTTPException(status_code=422, detail="discount_type must be 'flat' or 'percentage'")
    if "discount_value" in changes and (changes["discount_value"] or 0) < 0:
        raise HTTPException(status_code=422, detail="discount_value cannot be negative")
    if "max_uses" in changes and changes["max_uses"] is not None:
        if changes["max_uses"] < (coupon.used_count or 0):
            raise HTTPException(
                status_code=422,
                detail=(f"This code has already been used {coupon.used_count} times, "
                        f"so the limit cannot be set below that."),
            )

    for field, value in changes.items():
        setattr(coupon, field, value)
    db.commit()
    db.refresh(coupon)
    return coupon


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
