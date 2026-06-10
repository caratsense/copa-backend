"""
Admin Dashboard Routes
======================
- Live stats (today's orders, revenue, pending count)
- Revenue reports (daily/weekly/monthly)
- Advanced order filtering
- All require admin role
"""

from datetime import datetime, timedelta, timezone, date
from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session
from sqlalchemy import func, cast, Date

from app.db import get_db
from app.models.user import User
from app.models.order import Order, OrderStatus, PaymentStatus
from app.core.auth import require_admin
from app.schemas import DashboardStats, RevenueReport, OrderRead
from app.services.order_service import _enrich_order

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats", response_model=DashboardStats)
def get_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Real-time dashboard stats for today."""

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    today_orders = db.query(func.count(Order.id)).filter(Order.created_at >= today_start).scalar() or 0

    today_revenue = db.query(func.coalesce(func.sum(Order.total_price), 0.0)).filter(
        Order.created_at >= today_start,
        Order.status != OrderStatus.CANCELLED,
    ).scalar()

    pending = db.query(func.count(Order.id)).filter(
        Order.status.in_([OrderStatus.RECEIVED, OrderStatus.CONFIRMED])
    ).scalar() or 0

    in_production = db.query(func.count(Order.id)).filter(
        Order.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION, OrderStatus.QC, OrderStatus.PACKAGED])
    ).scalar() or 0

    out_for_delivery = db.query(func.count(Order.id)).filter(
        Order.status == OrderStatus.OUT_FOR_DELIVERY
    ).scalar() or 0

    delivered_today = db.query(func.count(Order.id)).filter(
        Order.status == OrderStatus.DELIVERED,
        Order.updated_at >= today_start,
    ).scalar() or 0

    cancelled_today = db.query(func.count(Order.id)).filter(
        Order.status == OrderStatus.CANCELLED,
        Order.updated_at >= today_start,
    ).scalar() or 0

    total_users = db.query(func.count(User.id)).scalar() or 0

    return DashboardStats(
        today_orders=today_orders,
        today_revenue=round(today_revenue, 2),
        pending_orders=pending,
        in_production_orders=in_production,
        out_for_delivery_orders=out_for_delivery,
        delivered_today=delivered_today,
        cancelled_today=cancelled_today,
        total_users=total_users,
    )


@router.get("/revenue", response_model=RevenueReport)
def get_revenue_report(
    period: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    days: int = Query(30, ge=1, le=365),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Revenue report grouped by day/week/month."""

    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        db.query(
            cast(Order.created_at, Date).label("day"),
            func.count(Order.id).label("orders"),
            func.coalesce(func.sum(Order.total_price), 0.0).label("revenue"),
        )
        .filter(
            Order.created_at >= since,
            Order.status != OrderStatus.CANCELLED,
        )
        .group_by(cast(Order.created_at, Date))
        .order_by(cast(Order.created_at, Date))
        .all()
    )

    data = [{"date": str(r.day), "orders": r.orders, "revenue": round(r.revenue, 2)} for r in rows]
    total_revenue = sum(d["revenue"] for d in data)
    total_orders = sum(d["orders"] for d in data)

    return RevenueReport(
        period=period,
        data=data,
        total_revenue=round(total_revenue, 2),
        total_orders=total_orders,
    )


@router.get("/orders", response_model=list[OrderRead])
def filter_orders(
    status: str | None = None,
    payment_status: str | None = None,
    user_id: int | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    skip: int = 0,
    limit: int = 50,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Advanced order filtering for admin dashboard."""

    q = db.query(Order)

    if status:
        q = q.filter(Order.status == status)
    if payment_status:
        q = q.filter(Order.payment_status == payment_status)
    if user_id:
        q = q.filter(Order.user_id == user_id)
    if date_from:
        q = q.filter(Order.created_at >= datetime.combine(date_from, datetime.min.time()))
    if date_to:
        q = q.filter(Order.created_at <= datetime.combine(date_to, datetime.max.time()))

    orders = q.order_by(Order.created_at.desc()).offset(skip).limit(limit).all()
    for o in orders:
        _enrich_order(o)
    return orders


@router.post("/process-queue")
def process_queued_orders(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """
    Manually trigger assignment of all queued orders (CONFIRMED but no baker).
    Useful if admin opens early or wants to process night orders immediately.
    """
    from app.services.assignment_engine import auto_assign_baker

    queued = (
        db.query(Order)
        .filter(
            Order.status == OrderStatus.CONFIRMED,
            Order.assigned_baker_id == None,
        )
        .order_by(Order.delivery_time.asc().nullslast(), Order.created_at.asc())
        .all()
    )

    assigned = 0
    failed = 0
    for order in queued:
        try:
            auto_assign_baker(db, order.id, force=True)
            assigned += 1
        except Exception:
            failed += 1
            break  # no bakers available

    return {
        "queued_total": len(queued),
        "assigned": assigned,
        "failed": failed,
        "message": f"Assigned {assigned} orders to bakers." if assigned else "No orders to assign or no bakers available.",
    }


@router.get("/queued-count")
def get_queued_count(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Get count of orders waiting in queue (CONFIRMED but no baker assigned)."""
    count = db.query(func.count(Order.id)).filter(
        Order.status == OrderStatus.CONFIRMED,
        Order.assigned_baker_id == None,
    ).scalar() or 0

    return {"queued_orders": count}
