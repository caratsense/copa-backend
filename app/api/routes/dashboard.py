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
from sqlalchemy import func, or_

from app.db import get_db
from app.models.user import User
from app.models.order import Order, OrderStatus, PaymentStatus
from app.core.auth import require_admin
from app.schemas import DashboardStats, RevenueReport, OrderRead
from app.services.order_service import _enrich_order

from app.services import stock_service

router = APIRouter(prefix="/dashboard", tags=["Dashboard"])


@router.get("/stats", response_model=DashboardStats)
def get_stats(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Real-time dashboard stats for today."""

    today_start = datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

    today_orders = db.query(func.count(Order.id)).filter(Order.created_at >= today_start).scalar() or 0

    # Revenue means money we can actually count on. An ONLINE order sitting at
    # PENDING is an abandoned checkout, not a sale; including it inflated the
    # figure every time someone bailed at the payment page.
    # Collected means paid. The COD clause that used to sit here counted
    # cash-on-delivery orders as revenue the moment they were placed; with COD
    # withdrawn, a legacy COD order only counts once someone marked it paid.
    _collected = Order.payment_status == PaymentStatus.PAID

    today_revenue = db.query(func.coalesce(func.sum(Order.total_price), 0.0)).filter(
        Order.created_at >= today_start,
        Order.status != OrderStatus.CANCELLED,
        _collected,
    ).scalar()

    today_unpaid = db.query(func.coalesce(func.sum(Order.total_price), 0.0)).filter(
        Order.created_at >= today_start,
        Order.status != OrderStatus.CANCELLED,
        ~_collected,
    ).scalar()

    # Work the shop actually has: placed, paid for, not started.
    #
    # This counted every RECEIVED or CONFIRMED row whatever its payment, so an
    # abandoned checkout — RECEIVED, never paid, nothing owed, nobody baking —
    # was counted as a job waiting. The dashboard said "3 orders" while the
    # orders screen, which does exclude unpaid attempts, showed none: two
    # screens disagreeing about the same orders, and the honest number was the
    # other one.
    #
    # Same rule as is_payable() in order_service, which is what actually gates
    # the kitchen: a legacy COD row is workable, an unpaid ONLINE one is not.
    workable = or_(
        func.upper(func.coalesce(Order.payment_method, "ONLINE")) == "COD",
        Order.payment_status == PaymentStatus.PAID,
    )
    pending = db.query(func.count(Order.id)).filter(
        Order.status.in_([OrderStatus.RECEIVED, OrderStatus.CONFIRMED]),
        workable,
    ).scalar() or 0

    in_production = db.query(func.count(Order.id)).filter(
        Order.status.in_([OrderStatus.ASSIGNED, OrderStatus.IN_PRODUCTION, OrderStatus.QC, OrderStatus.PACKAGED])
    ).scalar() or 0

    awaiting_approval = db.query(func.count(Order.id)).filter(
        Order.status == OrderStatus.AWAITING_APPROVAL
    ).scalar() or 0

    delivery_failed = db.query(func.count(Order.id)).filter(
        Order.status == OrderStatus.DELIVERY_FAILED
    ).scalar() or 0

    # stock is nullable: NULL means "not counted", so only finite rows count.
    from app.models.pricing import AddonRule
    low_stock = db.query(func.count(AddonRule.id)).filter(
        AddonRule.is_active == True,
        AddonRule.stock.isnot(None),
        AddonRule.stock <= stock_service.LOW_STOCK_AT,
        AddonRule.stock > 0,
    ).scalar() or 0

    # The numbers the tile actually shows. Products and extras can run out too
    # now, and a cake can be marked sold out with no count at all, so counting
    # add-on rows answers a narrower question than the one being asked.
    # Read from the same snapshot /admin/inventory serves, so the tile and the
    # screen cannot disagree.
    stock_counts = stock_service.counts(db)

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
        today_unpaid=round(today_unpaid, 2),
        pending_orders=pending,
        in_production_orders=in_production,
        awaiting_approval_orders=awaiting_approval,
        delivery_failed_orders=delivery_failed,
        low_stock_addons=low_stock,
        out_of_stock_items=stock_counts["out_of_stock_items"],
        low_stock_items=stock_counts["low_stock_items"],
        held_by_unpaid_units=stock_counts["held_by_unpaid_units"],
        out_for_delivery_orders=out_for_delivery,
        delivered_today=delivered_today,
        cancelled_today=cancelled_today,
        total_users=total_users,
    )


def _bucket(when: datetime, period: str) -> str:
    """
    Which day, week or month a sale belongs to, as an ISO date.

    A week is labelled by its Monday and a month by its first, so the label is
    a real date the caller can parse and sort rather than "2026-W46", which
    nothing downstream knows how to render.
    """
    day = when.date()
    if period == "weekly":
        return (day - timedelta(days=day.weekday())).isoformat()
    if period == "monthly":
        return day.replace(day=1).isoformat()
    return day.isoformat()


@router.get("/revenue", response_model=RevenueReport)
def get_revenue_report(
    period: str = Query("daily", pattern="^(daily|weekly|monthly)$"),
    days: int = Query(30, ge=1, le=365),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Revenue grouped by day, week or month.

    TWO THINGS WERE WRONG HERE.

    It grouped with `CAST(created_at AS DATE)`, which is a PostgreSQL answer to
    a question SQLite answers differently: SQLite has no DATE type, so the cast
    falls back to numeric affinity, reads the leading digits of
    "2026-08-12 10:00:00" and returns the integer 2026. Every row collapsed
    into one bucket called 2026 and the endpoint 500'd on the way out. In
    production, on Postgres, it worked - so this was invisible until somebody
    ran the admin dashboard locally, where it is the only screen that fails.

    And `period` was accepted, validated, echoed back in the response, and then
    ignored: the GROUP BY was by day whatever was asked for, so "monthly"
    returned a daily series labelled monthly. Nothing called it with anything
    but the default, which is the only reason that had not been noticed.

    Both go away by bucketing in Python. Day, week and month need date_trunc on
    Postgres and strftime on SQLite - three dialect-specific expressions to
    keep in step - whereas this is one implementation that provably agrees
    across both. The row count is bounded by `days` (365 at most) on a shop
    doing a handful of orders a day, and only two columns are fetched.

    Days are UTC days, which is what the cast did and what /stats still does.
    For a Lucknow bakery an IST boundary would be more honest - a 3am order
    currently counts as the day before - but that shifts every figure the
    owner has already seen, so it is a decision rather than a bug fix.
    """
    since = datetime.now(timezone.utc) - timedelta(days=days)

    rows = (
        db.query(Order.created_at, Order.total_price)
        .filter(
            Order.created_at >= since,
            Order.status != OrderStatus.CANCELLED,
            # Same rule as the dashboard tile: only money actually collected.
            Order.payment_status == PaymentStatus.PAID,
        )
        .all()
    )

    buckets: dict[str, dict] = {}
    for created_at, total in rows:
        if created_at is None:
            continue
        key = _bucket(created_at, period)
        bucket = buckets.setdefault(key, {"date": key, "orders": 0, "revenue": 0.0})
        bucket["orders"] += 1
        bucket["revenue"] += float(total or 0.0)

    # ISO dates sort lexicographically, so this is chronological.
    data = [
        {**bucket, "revenue": round(bucket["revenue"], 2)}
        for bucket in sorted(buckets.values(), key=lambda b: b["date"])
    ]

    return RevenueReport(
        period=period,
        data=data,
        total_revenue=round(sum(d["revenue"] for d in data), 2),
        total_orders=sum(d["orders"] for d in data),
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


@router.get("/whatsapp-outbox")
def whatsapp_outbox(
    status: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """
    Recent outbound WhatsApp notifications and what happened to them.

    Makes "the baker was never told" visible. A send that fails used to be a log
    line nobody reads; each attempt is now a row with its error and attempt count.
    """
    from app.models.whatsapp_message import WhatsAppMessage

    q = db.query(WhatsAppMessage)
    if status:
        q = q.filter(WhatsAppMessage.status == status.upper())
    rows = q.order_by(WhatsAppMessage.id.desc()).limit(limit).all()

    return {
        "messages": [
            {
                "id": m.id,
                "order_id": m.order_id,
                "template_key": m.template_key,
                "template_name": m.template_name,
                "recipient_role": m.recipient_role,
                "event_type": m.event_type,
                "status": m.status.value if hasattr(m.status, "value") else m.status,
                "attempts": m.attempts,
                "last_error": m.last_error,
                "created_at": m.created_at,
                "sent_at": m.sent_at,
            }
            for m in rows
        ]
    }


@router.post("/whatsapp-outbox/retry")
def retry_whatsapp_outbox(
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Re-attempt notifications that failed but have attempts remaining."""
    from app.services.wa_outbox import retry_pending

    return retry_pending(db)


@router.get("/whatsapp-templates")
def whatsapp_templates(admin: User = Depends(require_admin)):
    """
    The Meta templates this deployment expects to exist.

    Handover aid: whoever builds the templates in WhatsApp Manager can read the
    exact names, languages, recipients and placeholder counts straight from the
    running service rather than from a document that drifts.
    """
    from app.services.wa_templates import describe_all

    return {"templates": describe_all()}


@router.get("/queued-count")
def get_queued_count(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Get count of orders waiting in queue (CONFIRMED but no baker assigned)."""
    count = db.query(func.count(Order.id)).filter(
        Order.status == OrderStatus.CONFIRMED,
        Order.assigned_baker_id == None,
    ).scalar() or 0

    return {"queued_orders": count}
