"""
PayU Payment Gateway (India) — hosted checkout
==============================================
Flow:
  1. POST /payments/create-order  → build signed PayU params; frontend auto-POSTs
     a hidden form to PayU's hosted payment page.
  2. PayU processes the payment, then POSTs the result to our /payments/payu-callback.
  3. We verify the reverse hash, mark the order PAID/FAILED, and 303-redirect the
     browser back to the app's orders page.

Config (.env):
  PAYU_KEY, PAYU_SALT, PAYU_ENV=test|prod, BACKEND_BASE_URL (this API's public URL)
  When PAYU_KEY/SALT are empty → demo mode (payment auto-marked paid).
"""

import hashlib
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
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

PAYU_URL = "https://secure.payu.in/_payment" if settings.PAYU_ENV == "prod" else "https://test.payu.in/_payment"


def _sha512(*parts) -> str:
    return hashlib.sha512("|".join(str(p) for p in parts).encode()).hexdigest()


def _frontend_base() -> str:
    # Reuse the existing frontend URL config (WHATSAPP_TRACKING_BASE_URL ends in /track)
    return settings.WHATSAPP_TRACKING_BASE_URL.replace("/track", "").rstrip("/")


def _order_id_from_txnid(txnid: str):
    # txnid format: CO<order_id>-<random>
    if txnid.startswith("CO") and "-" in txnid:
        try:
            return int(txnid[2:].split("-")[0])
        except ValueError:
            return None
    return None


class PaymentOrderRequest(BaseModel):
    order_id: int
    payment_method: str = "ONLINE"  # ONLINE or COD


@router.post("/create-order")
def create_payment_order(
    data: PaymentOrderRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a PayU payment (or register COD). Returns params for the frontend to POST to PayU."""
    order = db.query(Order).filter(Order.id == data.order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")

    # COD — just record it and return
    if data.payment_method == "COD":
        order.payment_method = "COD"
        order.payment_status = PaymentStatus.COD_PENDING
        db.commit()
        return {"status": "cod", "message": "Order placed with Cash on Delivery"}

    # ONLINE — demo mode when PayU isn't configured
    if not settings.PAYU_KEY or not settings.PAYU_SALT:
        order.payment_method = "ONLINE"
        order.payment_status = PaymentStatus.PAID
        order.payment_id = f"DEMO_{order.id}"
        db.commit()
        return {"status": "demo_paid", "message": "Demo mode — payment simulated as paid"}

    txnid = f"CO{order.id}-{secrets.token_hex(4)}"
    amount = f"{float(order.total_price):.2f}"
    productinfo = f"Cake O Clock Order {order.id}"
    firstname = (user.name or "Customer").split(" ")[0]
    email = user.email or "orders@cakeoclock.in"
    phone = (user.phone or "").replace("+", "") or "9999999999"

    callback = f"{settings.BACKEND_BASE_URL.rstrip('/')}/payments/payu-callback"

    # Request hash: key|txnid|amount|productinfo|firstname|email|udf1..5|||||| |salt (udf empty)
    hash_ = _sha512(
        settings.PAYU_KEY, txnid, amount, productinfo, firstname, email,
        "", "", "", "", "", "", "", "", "", "", settings.PAYU_SALT,
    )

    order.payment_method = "ONLINE"
    order.payment_id = txnid
    db.commit()

    return {
        "status": "created",
        "action": PAYU_URL,
        "params": {
            "key": settings.PAYU_KEY,
            "txnid": txnid,
            "amount": amount,
            "productinfo": productinfo,
            "firstname": firstname,
            "email": email,
            "phone": phone,
            "surl": callback,
            "furl": callback,
            "hash": hash_,
        },
    }


@router.post("/payu-callback")
async def payu_callback(request: Request, db: Session = Depends(get_db)):
    """PayU posts the payment result here (browser navigation). Verify + redirect to the app."""
    form = await request.form()
    d = {k: str(v) for k, v in form.items()}
    status = d.get("status", "")
    txnid = d.get("txnid", "")
    order_id = _order_id_from_txnid(txnid)
    front = _frontend_base()

    # Reverse hash: salt|status|udf10..1|email|firstname|productinfo|amount|txnid|key (udf empty)
    expected = _sha512(
        settings.PAYU_SALT, status,
        "", "", "", "", "", "", "", "", "", "",
        d.get("email", ""), d.get("firstname", ""), d.get("productinfo", ""),
        d.get("amount", ""), txnid, settings.PAYU_KEY,
    )

    if order_id and status == "success" and d.get("hash", "") == expected:
        order = db.query(Order).filter(Order.id == order_id).first()
        if order:
            order.payment_status = PaymentStatus.PAID
            db.commit()
        return RedirectResponse(url=f"{front}/orders?success={order_id}", status_code=303)

    logger.warning(f"[PAYU] Payment not confirmed (status={status}, txnid={txnid})")
    return RedirectResponse(url=f"{front}/checkout?payment=failed", status_code=303)


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
