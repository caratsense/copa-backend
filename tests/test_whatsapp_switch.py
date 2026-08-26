"""
WHATSAPP_ENABLED is a kill switch in BOTH directions.

It used to gate only outbound sending, so a deployment with the integration
"disabled" would still accept an inbound `DONE 145` from anyone who could reach
the webhook and move a real order. Turning WhatsApp off must mean off.

Pinned here because this is the lever someone reaches for during an incident,
and it has to behave the way the runbook says it does:

    false  no outbound sends · no inbound state changes · webhook still 200s
           (Meta must not see errors and start retrying) · website unaffected
    true   signed webhooks process commands · outbound templates send
"""

import hashlib
import hmac
import json

import pytest

from app.models.order import OrderStatus
from app.models.user import UserRole
from app.models.whatsapp_message import WhatsAppMessage, WhatsAppMessageStatus

from tests.conftest import auth, make_order, make_user


def _signed(client, secret: str, body: dict):
    raw = json.dumps(body).encode()
    sig = hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return client.post("/webhook/whatsapp", content=raw,
                       headers={"X-Hub-Signature-256": f"sha256={sig}",
                                "Content-Type": "application/json"})


def _inbound(sender: str, text: str, msg_id: str = "wamid.SWITCH1"):
    return {"entry": [{"changes": [{"value": {"messages": [
        {"from": sender, "id": msg_id, "type": "text", "text": {"body": text}}
    ]}}]}]}


@pytest.fixture
def baker_with_order(db):
    """
    A baker mid-bake, plus the admin who receives the quality-check message.

    The admin matters: completing a bake notifies the ADMIN, not the baker, so
    without one there is no outbound message to suppress or record.
    """
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+918888888881")
    baker.whatsapp_opt_in = True
    admin = make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462")
    admin.whatsapp_opt_in = True
    customer = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    customer.whatsapp_opt_in = True
    order = make_order(db, customer, OrderStatus.IN_PRODUCTION)
    order.assigned_baker_id = baker.id
    db.commit()
    db.refresh(order)
    return baker, order


# ─── DISABLED ────────────────────────────────────────

def test_disabled_refuses_inbound_state_changes(client, db, wa_secret, baker_with_order):
    baker, order = baker_with_order
    wa_secret.WHATSAPP_ENABLED = False

    r = _signed(client, "test-app-secret", _inbound("918888888881", f"DONE {order.id}"))

    assert r.status_code == 200, "Meta must not be given an error to retry"
    db.refresh(order)
    assert order.status == OrderStatus.IN_PRODUCTION, \
        "a disabled integration moved a real order"


def test_disabled_still_acknowledges_the_webhook(client, wa_secret):
    """A non-200 makes Meta retry and eventually disable the subscription."""
    wa_secret.WHATSAPP_ENABLED = False
    r = _signed(client, "test-app-secret", _inbound("919999999999", "hello"))
    assert r.status_code == 200


def test_disabled_still_verifies_signatures(client, wa_secret):
    """Off is not a reason to stop authenticating what arrives."""
    wa_secret.WHATSAPP_ENABLED = False
    r = client.post("/webhook/whatsapp", json=_inbound("918888888881", "DONE 1"),
                    headers={"X-Hub-Signature-256": "sha256=deadbeef"})
    assert r.status_code == 403


def test_disabled_sends_nothing_outbound(client, db, wa_secret, baker_with_order):
    """The order still moves from the website; only the message is suppressed."""
    baker, order = baker_with_order
    wa_secret.WHATSAPP_ENABLED = False

    res = client.post(f"/baker/orders/{order.id}/baking-done", headers=auth(baker))
    assert res.status_code == 200

    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL, "the website must keep working"

    rows = db.query(WhatsAppMessage).filter(WhatsAppMessage.order_id == order.id).all()
    assert rows, "the intent should still be recorded"
    assert all(m.status == WhatsAppMessageStatus.SKIPPED for m in rows), \
        "something was sent while WhatsApp was disabled"
    assert any("WHATSAPP_ENABLED" in (m.last_error or "") for m in rows), \
        "the reason for skipping should say which switch caused it"


# ─── ENABLED ─────────────────────────────────────────

def test_enabled_processes_a_signed_command(client, db, wa_secret, baker_with_order):
    baker, order = baker_with_order
    assert wa_secret.WHATSAPP_ENABLED is True

    r = _signed(client, "test-app-secret", _inbound("918888888881", f"DONE {order.id}"))

    assert r.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL


def test_enabled_records_a_send(client, db, wa_secret, baker_with_order):
    baker, order = baker_with_order
    client.post(f"/baker/orders/{order.id}/baking-done", headers=auth(baker))

    rows = db.query(WhatsAppMessage).filter(WhatsAppMessage.order_id == order.id).all()
    assert any(m.status == WhatsAppMessageStatus.SENT for m in rows)


# ─── META 5xx ────────────────────────────────────────

def test_meta_server_error_is_recorded_and_retryable(client, db, wa_secret,
                                                     baker_with_order, monkeypatch):
    """
    A 5xx is transient, so it must land as FAILED with attempts left — not as a
    terminal SKIPPED, and never by rolling back the order.
    """
    from app.services import wa_outbox, whatsapp_sender

    baker, order = baker_with_order
    monkeypatch.setattr(whatsapp_sender, "_send",
                        lambda p: {"error": {"message": "Internal server error", "code": 500}})

    res = client.post(f"/baker/orders/{order.id}/baking-done", headers=auth(baker))
    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL, "a Meta 5xx rolled back a real transition"

    rows = db.query(WhatsAppMessage).filter(
        WhatsAppMessage.order_id == order.id,
        WhatsAppMessage.status == WhatsAppMessageStatus.FAILED).all()
    assert rows, "a 5xx was not recorded as retryable"
    assert all(m.attempts < wa_outbox.MAX_ATTEMPTS for m in rows)

    # Meta recovers; the backlog drains.
    monkeypatch.setattr(whatsapp_sender, "_send", lambda p: {"messages": [{"id": "wamid.OK"}]})
    result = wa_outbox.retry_pending(db)
    assert result["sent"] >= 1
