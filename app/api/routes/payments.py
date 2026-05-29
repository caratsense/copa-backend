"""
Cashfree Payment Gateway
=========================
Create payment orders, verify payments, check status.
"""

import httpx
import logging
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.order import Order, PaymentStatus
from app.models.user import User
from app.core.auth import get_current_user
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])

CASHFREE_BASE = "https://sandbox.cashfree.com/pg" if settings.CASHFREE_ENV == "sandbox" else "https://api.cashfree.com/pg"


class PaymentOrderRequest(BaseModel):
    order_id: int
    payment_method: str = "ONLINE"  # ONLINE or COD


class PaymentVerifyRequest(BaseModel):
    order_id: int
    cashfree_order_id: str | None = None


@router.post("/create-order")
def create_payment_order(
    data: PaymentOrderRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a Cashfree payment order or mark as COD."""
    order = db.query(Order).filter(Order.id == data.order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # COD — just mark it and return
    if data.payment_method == "COD":
        order.payment_method = "COD"
        order.payment_status = PaymentStatus.COD_PENDING
        db.commit()
        return {"status": "cod", "message": "Order placed with Cash on Delivery"}

    # ONLINE — create Cashfree order
    if not settings.CASHFREE_APP_ID or not settings.CASHFREE_SECRET_KEY:
        # Sandbox/demo mode — simulate payment success
        order.payment_method = "ONLINE"
        order.payment_status = PaymentStatus.PAID
        order.payment_id = f"DEMO_{order.id}"
        db.commit()
        return {"status": "demo_paid", "message": "Demo mode — payment simulated as paid"}

    try:
        cf_order = {
            "order_id": f"COPA_{order.id}",
            "order_amount": float(order.total_price),
            "order_currency": "INR",
            "customer_details": {
                "customer_id": str(user.id),
                "customer_name": user.name or "Customer",
                "customer_phone": user.phone.replace("+", "") if user.phone else "9999999999",
                "customer_email": user.email or "customer@cakeoclock.com",
            },
            "order_meta": {
                "return_url": f"{settings.WHATSAPP_TRACKING_BASE_URL.replace('/track', '')}/orders?payment=success&order_id={order.id}",
            },
        }

        resp = httpx.post(
            f"{CASHFREE_BASE}/orders",
            headers={
                "x-client-id": settings.CASHFREE_APP_ID,
                "x-client-secret": settings.CASHFREE_SECRET_KEY,
                "x-api-version": "2023-08-01",
                "Content-Type": "application/json",
            },
            json=cf_order,
            timeout=15,
        )
        result = resp.json()

        if resp.status_code >= 400:
            logger.error(f"[CASHFREE] Order creation failed: {result}")
            raise HTTPException(status_code=400, detail="Payment order creation failed")

        order.payment_method = "ONLINE"
        order.payment_id = result.get("cf_order_id", "")
        db.commit()

        return {
            "status": "created",
            "payment_session_id": result.get("payment_session_id"),
            "cf_order_id": result.get("cf_order_id"),
            "order_id": order.id,
            "environment": settings.CASHFREE_ENV or "sandbox",
        }

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[CASHFREE] Error: {e}")
        raise HTTPException(status_code=500, detail="Payment service error")


@router.post("/verify")
def verify_payment(
    data: PaymentVerifyRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Verify payment status with Cashfree."""
    order = db.query(Order).filter(Order.id == data.order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    if not settings.CASHFREE_APP_ID:
        # Demo mode
        return {"status": "paid", "payment_status": "PAID"}

    try:
        resp = httpx.get(
            f"{CASHFREE_BASE}/orders/COPA_{order.id}",
            headers={
                "x-client-id": settings.CASHFREE_APP_ID,
                "x-client-secret": settings.CASHFREE_SECRET_KEY,
                "x-api-version": "2023-08-01",
            },
            timeout=15,
        )
        result = resp.json()
        cf_status = result.get("order_status", "")

        if cf_status == "PAID":
            order.payment_status = PaymentStatus.PAID
            db.commit()
            return {"status": "paid", "payment_status": "PAID"}
        elif cf_status == "EXPIRED":
            order.payment_status = PaymentStatus.FAILED
            db.commit()
            return {"status": "failed", "payment_status": "FAILED"}
        else:
            return {"status": "pending", "payment_status": order.payment_status.value}

    except Exception as e:
        logger.error(f"[CASHFREE] Verify error: {e}")
        return {"status": "error", "payment_status": order.payment_status.value}


@router.get("/status/{order_id}")
def payment_status(
    order_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check payment status of an order."""
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    return {
        "order_id": order.id,
        "payment_status": order.payment_status.value,
        "payment_method": order.payment_method,
        "payment_id": order.payment_id,
    }
