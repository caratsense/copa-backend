"""
Admin Staff Management Routes
===============================
- Create baker/rider accounts (with temp password)
- List all staff with workload
- Toggle duty status for any staff
- Deactivate/activate staff
- View any baker/rider's queue
- Reassign orders
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.db import get_db
from app.models.user import User, UserRole
from app.models.order import Order, OrderStatus
from app.core.auth import require_admin, hash_password
from app.schemas import StaffCreate, StaffRead, DutyToggle, OrderRead
from app.services.assignment_engine import admin_assign_baker

router = APIRouter(prefix="/admin/staff", tags=["Admin — Staff Management"])


@router.post("", response_model=StaffRead, status_code=201)
def create_staff(
    data: StaffCreate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Admin creates a baker or rider account with a temporary password.
    Share the password with the staff member — they log in via /auth/login.
    """
    if data.role not in ("baker", "rider"):
        raise HTTPException(status_code=400, detail="Role must be 'baker' or 'rider'")

    existing = db.query(User).filter(User.phone == data.phone).first()
    if existing:
        raise HTTPException(status_code=409, detail="Phone number already registered")

    user = User(
        name=data.name,
        phone=data.phone,
        email=data.email,
        password_hash=hash_password(data.password),
        role=UserRole(data.role),
        on_duty=True,
    )
    db.add(user)
    db.commit()
    db.refresh(user)

    return _staff_with_count(db, user)


@router.get("", response_model=list[StaffRead])
def list_staff(
    role: str | None = None,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """List all staff (bakers + riders) with their current workload."""
    q = db.query(User).filter(User.role.in_([UserRole.BAKER, UserRole.RIDER]))
    if role:
        q = q.filter(User.role == role)

    staff = q.order_by(User.name).all()
    return [_staff_with_count(db, s) for s in staff]


@router.get("/{staff_id}", response_model=StaffRead)
def get_staff(staff_id: int, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    user = db.query(User).filter(
        User.id == staff_id,
        User.role.in_([UserRole.BAKER, UserRole.RIDER]),
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Staff member not found")
    return _staff_with_count(db, user)


@router.patch("/{staff_id}/duty", response_model=StaffRead)
def toggle_staff_duty(
    staff_id: int,
    data: DutyToggle,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin toggles duty status for any baker/rider."""
    user = db.query(User).filter(
        User.id == staff_id,
        User.role.in_([UserRole.BAKER, UserRole.RIDER]),
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Staff member not found")

    user.on_duty = data.on_duty
    db.commit()
    db.refresh(user)
    return _staff_with_count(db, user)


@router.patch("/{staff_id}/activate", response_model=StaffRead)
def toggle_staff_active(
    staff_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin activates/deactivates a staff account."""
    user = db.query(User).filter(
        User.id == staff_id,
        User.role.in_([UserRole.BAKER, UserRole.RIDER]),
    ).first()
    if not user:
        raise HTTPException(status_code=404, detail="Staff member not found")

    user.is_active = not user.is_active
    if not user.is_active:
        user.on_duty = False  # deactivated = off duty
    db.commit()
    db.refresh(user)
    return _staff_with_count(db, user)


@router.get("/{staff_id}/queue", response_model=list[OrderRead])
def get_staff_queue(
    staff_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin views any baker/rider's active orders."""
    user = db.query(User).filter(User.id == staff_id).first()
    if not user:
        raise HTTPException(status_code=404, detail="Staff member not found")

    if user.role == UserRole.BAKER:
        statuses = [OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION, OrderStatus.QC]
        field = Order.assigned_baker_id
    else:
        statuses = [OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY]
        field = Order.assigned_rider_id

    return (
        db.query(Order)
        .filter(field == staff_id, Order.status.in_(statuses))
        .order_by(Order.created_at.asc())
        .all()
    )


@router.post("/orders/{order_id}/reassign-baker", response_model=OrderRead)
def reassign_baker(
    order_id: int,
    baker_id: int,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin manually reassigns an order to a different baker."""
    return admin_assign_baker(db, order_id, baker_id)


# ─── HELPER ──────────────────────────────────────────

def _staff_with_count(db: Session, user: User) -> StaffRead:
    """Build StaffRead with active order count."""
    if user.role == UserRole.BAKER:
        statuses = [OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION, OrderStatus.QC]
        count = db.query(func.count(Order.id)).filter(
            Order.assigned_baker_id == user.id,
            Order.status.in_(statuses),
        ).scalar() or 0
    else:
        statuses = [OrderStatus.PACKAGED, OrderStatus.OUT_FOR_DELIVERY]
        count = db.query(func.count(Order.id)).filter(
            Order.assigned_rider_id == user.id,
            Order.status.in_(statuses),
        ).scalar() or 0

    return StaffRead(
        id=user.id,
        name=user.name,
        phone=user.phone,
        email=user.email,
        role=user.role.value,
        is_active=user.is_active,
        on_duty=user.on_duty,
        active_order_count=count,
    )
