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

  When PAYU_KEY/SALT are empty, online payment FAILS CLOSED (503) and the
  callback refuses to settle anything. Simulated payments require an explicit
  PAYU_ALLOW_DEMO_PAYMENTS=true and are for local development only — they used
  to be the automatic fallback, which meant an unconfigured production deploy
  marked every ONLINE order PAID without collecting a rupee.
"""

import hashlib
import hmac
import logging
import secrets

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.order import Order, PaymentStatus
from app.models.user import User, UserRole
from app.core.auth import get_current_user
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

router = APIRouter(prefix="/payments", tags=["Payments"])

PAYU_URL = "https://secure.payu.in/_payment" if settings.PAYU_ENV == "prod" else "https://test.payu.in/_payment"


def _sha512(*parts) -> str:
    return hashlib.sha512("|".join(str(p) for p in parts).encode()).hexdigest()


def _frontend_base() -> str:
    """Where PayU sends the customer back to. See Settings.frontend_base_url."""
    return settings.frontend_base_url


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


def _get_own_order(db: Session, order_id: int, user: User) -> Order:
    """Fetch an order, 404 if missing, 403 if it isn't the caller's (admins exempt).

    Returning 404 for someone else's order would also be defensible, but the
    order id is already visible to its owner, so 403 is clearer here.
    """
    order = db.query(Order).filter(Order.id == order_id).first()
    if not order:
        raise HTTPException(status_code=404, detail="Order not found")
    if order.user_id != user.id and user.role != UserRole.ADMIN:
        raise HTTPException(status_code=403, detail="This order does not belong to you")
    return order


def _lock_order(db: Session, order_id: int) -> Order | None:
    """
    Select the order FOR UPDATE so concurrent callbacks serialise.

    SQLite has no row locks and ignores the clause, which is fine for tests;
    Postgres — where the money actually moves — honours it.
    """
    q = db.query(Order).filter(Order.id == order_id)
    try:
        return q.with_for_update().first()
    except Exception:
        return q.first()


def _release_for_production(db: Session, order_id: int) -> None:
    """
    Hand a now-payable order to a baker.

    Orders no longer auto-assign at creation, because that put unpaid ONLINE
    orders straight into the kitchen. Assignment happens here instead, once the
    order is genuinely payable, and goes through the normal engine so the baker
    is notified exactly as they would be from the dashboard.
    """
    from app.services.assignment_engine import auto_assign_baker

    try:
        auto_assign_baker(db, order_id)
    except HTTPException as e:
        # No baker free is an operations problem, not a payment failure — the
        # money is taken and the admin queue will surface it.
        logger.info("[PAYU] Order %s paid but not assigned yet: %s", order_id, e.detail)
    except Exception as e:
        logger.error("[PAYU] Assignment after payment failed for order %s: %s", order_id, e)


@router.post("/create-order")
def create_payment_order(
    data: PaymentOrderRequest,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Create a PayU payment (or register COD). Returns params for the frontend to POST to PayU."""
    order = _get_own_order(db, data.order_id, user)

    # Already-paid orders must not be re-opened for payment.
    if order.payment_status == PaymentStatus.PAID:
        raise HTTPException(status_code=400, detail="This order is already paid")

    # COD — record it and release the order for production.
    if data.payment_method == "COD":
        order.payment_method = "COD"
        order.payment_status = PaymentStatus.COD_PENDING
        db.commit()
        _release_for_production(db, order.id)
        return {"status": "cod", "message": "Order placed with Cash on Delivery"}

    # ONLINE without PayU configured.
    if not settings.PAYU_KEY or not settings.PAYU_SALT:
        if not settings.PAYU_ALLOW_DEMO_PAYMENTS:
            # FAIL CLOSED. This branch used to mark the order PAID and return
            # "demo_paid" — so an unconfigured production deploy handed out
            # every cake for free, silently, with no payment ever taken.
            logger.error(
                "[PAYU] Payment requested for order %s but PAYU_KEY/PAYU_SALT are unset "
                "and PAYU_ALLOW_DEMO_PAYMENTS is off. Refusing to simulate payment.",
                order.id,
            )
            raise HTTPException(
                status_code=503,
                detail="Online payment is temporarily unavailable. Please choose Cash on Delivery or try again shortly.",
            )
        logger.warning("[PAYU] DEMO MODE: simulating payment for order %s", order.id)
        order.payment_method = "ONLINE"
        order.payment_status = PaymentStatus.PAID
        order.payment_id = f"DEMO_{order.id}"
        db.commit()
        _release_for_production(db, order.id)
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
    """
    PayU posts the payment result here (browser navigation).

    Everything about this request is attacker-controlled, so each of these is
    checked before a rupee is considered received:
      * PayU must be configured at all (an empty salt makes the hash forgeable
        by anyone, since they would know every input);
      * the reverse hash must match, compared constant-time;
      * the transaction id must be the one WE issued for this order;
      * the amount must equal what the order actually costs;
      * an order already settled is left alone, so repeated callbacks are safe.
    """
    form = await request.form()
    d = {k: str(v) for k, v in form.items()}
    status = d.get("status", "")
    txnid = d.get("txnid", "")
    order_id = _order_id_from_txnid(txnid)
    front = _frontend_base()

    def _fail(reason: str, **extra):
        logger.warning("[PAYU] Callback rejected (%s) txnid=%s %s", reason, txnid, extra or "")
        return RedirectResponse(url=f"{front}/checkout?payment=failed", status_code=303)

    # Without a salt the reverse hash proves nothing: an attacker knows every
    # input and can compute it themselves.
    if not settings.PAYU_KEY or not settings.PAYU_SALT:
        logger.error("[PAYU] Callback received while PayU is unconfigured — refusing to settle")
        return _fail("payu not configured")

    if not order_id:
        return _fail("unparseable txnid")

    expected = _sha512(
        settings.PAYU_SALT, status,
        "", "", "", "", "", "", "", "", "", "",
        d.get("email", ""), d.get("firstname", ""), d.get("productinfo", ""),
        d.get("amount", ""), txnid, settings.PAYU_KEY,
    )
    if not hmac.compare_digest(d.get("hash", ""), expected):
        return _fail("bad hash", order_id=order_id)

    if status != "success":
        return _fail(f"status={status}", order_id=order_id)

    # Lock the row: PayU can deliver the same callback more than once, and the
    # customer may also be refreshing the return page.
    order = _lock_order(db, order_id)
    if not order:
        return _fail("unknown order", order_id=order_id)

    # Already settled — treat a repeat as success without touching anything.
    if order.payment_status == PaymentStatus.PAID:
        logger.info("[PAYU] Duplicate callback for already-paid order %s ignored", order_id)
        return RedirectResponse(url=f"{front}/orders?success={order_id}", status_code=303)

    # The txnid must be the one this service issued for this order.
    if not order.payment_id or not hmac.compare_digest(order.payment_id, txnid):
        db.rollback()
        return _fail("txnid does not match the order", order_id=order_id,
                     expected=order.payment_id)

    # And the amount must be what the order actually costs.
    try:
        paid = round(float(d.get("amount", "0")), 2)
    except ValueError:
        db.rollback()
        return _fail("unparseable amount", order_id=order_id)
    owed = round(float(order.total_price or 0), 2)
    if paid != owed:
        db.rollback()
        return _fail("amount mismatch", order_id=order_id, paid=paid, owed=owed)

    order.payment_status = PaymentStatus.PAID
    db.commit()
    logger.info("[PAYU] Order %s settled for %.2f (txnid %s)", order_id, paid, txnid)

    _release_for_production(db, order_id)
    return RedirectResponse(url=f"{front}/orders?success={order_id}", status_code=303)


@router.get("/status/{order_id}")
def payment_status(
    order_id: int,
    user: User = Depends(get_current_user),
    db: Session = Depends(get_db),
):
    """Check payment status of an order. Own orders only (admins can see any)."""
    order = _get_own_order(db, order_id, user)
    return {
        "order_id": order.id,
        "payment_status": order.payment_status.value,
        "payment_method": order.payment_method,
        "payment_id": order.payment_id,
    }
