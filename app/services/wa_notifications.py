"""
WhatsApp Notification Dispatcher
==================================
Called after every order status change.
Sends WhatsApp notifications to relevant people.
"""

import logging
from sqlalchemy.orm import Session
from app.models.order import Order, OrderStatus
from app.models.user import User, UserRole
from app.services.order_service import _enrich_order
from app.services import whatsapp_sender as wa
from app.config import get_settings
settings = get_settings()

logger = logging.getLogger(__name__)


def _items_str(order: Order) -> str:
    """Build a human-readable items string."""
    parts = []
    for item in (order.items or []):
        c = item.customization or {}
        parts.append(f"{c.get('size', '1kg')} {c.get('flavor', '')} {item.product.name if item.product else 'Cake'} ×{item.quantity}")
    return ", ".join(parts) or "Cake"


def _delivery_str(order: Order) -> str:
    """Build delivery time string."""
    if order.delivery_time:
        from datetime import datetime
        dt = order.delivery_time
        return dt.strftime("%d %b, %I:%M %p")
    return "ASAP"


def _maps_link(address: str) -> str:
    """Generate Google Maps link from address."""
    if not address or address == "Self Pickup":
        return ""
    return f"https://www.google.com/maps/search/?api=1&query={address.replace(' ', '+')}"


def dispatch_notifications(db: Session, order: Order, new_status: str):
    """Send WhatsApp notifications based on the new status."""
    if not settings.WHATSAPP_ENABLED:
        logger.info(f"[WA DISPATCH] Disabled. Would notify for order #{order.id} → {new_status}")
        return

    _enrich_order(order)
    items = _items_str(order)
    delivery = _delivery_str(order)

    try:
        # ─── CONFIRMED → notify customer + admin ──────
        if new_status == OrderStatus.CONFIRMED.value:
            if order.user and order.user.phone:
                wa.notify_customer_order_confirmed(
                    order.user.phone, order.customer_name or "Customer",
                    order.id, items, order.total_price, delivery,
                )
            # Notify all admins
            admins = db.query(User).filter(User.role == UserRole.ADMIN).all()
            for admin in admins:
                wa.notify_admin_new_order(
                    admin.phone, order.id,
                    order.customer_name or "Customer",
                    order.user.phone if order.user else "",
                    items, order.total_price, delivery,
                )

        # ─── ASSIGNED → notify baker ─────────────────
        elif new_status == OrderStatus.ASSIGNED.value:
            if order.baker and order.baker.phone:
                wa.notify_baker_new_order(
                    order.baker.phone, order.id, items,
                    order.notes or "None", delivery,
                )

        # ─── AWAITING_APPROVAL → notify admin ────────
        elif new_status == OrderStatus.AWAITING_APPROVAL.value:
            admins = db.query(User).filter(User.role == UserRole.ADMIN).all()
            for admin in admins:
                wa.notify_admin_approval_needed(
                    admin.phone, order.id, items, order.total_price,
                )

        # ─── PACKAGED → if rider assigned, notify rider
        elif new_status == OrderStatus.PACKAGED.value:
            if order.rider and order.rider.phone:
                address = order.delivery_address or "Self Pickup"
                wa.notify_rider_new_delivery(
                    order.rider.phone, order.id,
                    order.customer_name or "Customer",
                    order.user.phone if order.user else "",
                    address, _maps_link(address),
                    order.total_price,
                )

        # ─── DELIVERED → notify customer + admin ─────
        elif new_status == OrderStatus.DELIVERED.value:
            if order.user and order.user.phone:
                wa.notify_customer_delivered(
                    order.user.phone, order.customer_name or "Customer",
                    order.id,
                )
            admins = db.query(User).filter(User.role == UserRole.ADMIN).all()
            for admin in admins:
                wa.send_text(admin.phone, f"✅ Order #{order.id} delivered to {order.customer_name or 'Customer'}")

        # ─── CANCELLED → notify customer + assigned staff
        elif new_status == OrderStatus.CANCELLED.value:
            if order.user and order.user.phone:
                wa.notify_customer_cancelled(
                    order.user.phone, order.customer_name or "Customer",
                    order.id,
                )
            if order.baker and order.baker.phone:
                wa.send_text(order.baker.phone, f"❌ Order #{order.id} has been cancelled.")
            if order.rider and order.rider.phone:
                wa.send_text(order.rider.phone, f"❌ Order #{order.id} has been cancelled. Delivery not needed.")

        # ─── IN_PRODUCTION (rejected) → notify baker ─
        elif new_status == OrderStatus.IN_PRODUCTION.value:
            # This could be baker starting OR admin rejecting
            # If order was AWAITING_APPROVAL before, it's a rejection
            if order.baker and order.baker.phone:
                wa.send_text(order.baker.phone, f"⚠️ Order #{order.id} sent back for rework. Please check and reply START {order.id} when ready.")

    except Exception as e:
        logger.error(f"[WA DISPATCH] Error for order #{order.id}: {e}")
