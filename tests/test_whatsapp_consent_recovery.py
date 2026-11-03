"""
Consent you can give after the fact, and a shop that can see who is unreachable.

Shriya placed order #36 from her own number and was never told anything about
it. Nothing had failed: her account was created by seeding, so it carried no
WhatsApp opt-in, and every notification for that order was skipped exactly as
designed. The outbox said so in as many words - SKIPPED, "no opt-in on record".

Seventeen messages had gone that way, across four customers with real orders.
None of them had opted OUT; none had ever been asked. The gate is right and
stays. What was wrong is that the state was write-only: captured once at
registration, never read back by anything, with no way to change it afterwards
and nothing to tell either side it was costing them their order updates.

These pin the two halves of the way out: the person can give consent
themselves, and the shop can see when they have not.
"""

import pytest

from app.models.order import OrderStatus
from app.models.user import UserRole
from app.services.wa_consent import may_message

from tests.conftest import auth, make_order, make_user


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    from app.main import app, limiter as app_limiter

    monkeypatch.setattr(app_limiter, "enabled", False)
    if hasattr(app.state, "limiter"):
        monkeypatch.setattr(app.state.limiter, "enabled", False)
    yield


# ─── GIVING CONSENT AFTER REGISTRATION ────────────────

def test_a_customer_can_opt_in_after_registering(client, db):
    """The case with no route out of it: unticked at signup, silent for good."""
    user = make_user(db, "Kavya", UserRole.CUSTOMER, phone="+919111100001")
    user.whatsapp_opt_in = False
    db.commit()
    assert may_message(user) is False

    res = client.post("/auth/whatsapp-consent",
                      json={"opt_in": True, "source": "checkout"},
                      headers=auth(user))

    assert res.status_code == 200, res.text
    assert res.json()["whatsapp_opt_in"] is True
    db.refresh(user)
    assert may_message(user) is True
    assert user.whatsapp_opt_in_source == "checkout"
    assert user.whatsapp_opt_in_at is not None


def test_opting_in_records_when_and_how(client, db):
    """Meta can ask to see consent. An opt-in with no provenance is no use."""
    user = make_user(db, "Kavya", UserRole.CUSTOMER, phone="+919111100002")
    user.whatsapp_opt_in = False
    db.commit()

    client.post("/auth/whatsapp-consent", json={"opt_in": True, "source": "account_page"},
                headers=auth(user))

    db.refresh(user)
    assert user.whatsapp_opt_in_source == "account_page"
    assert user.whatsapp_opt_in_at is not None


def test_an_invented_source_is_refused(client, db):
    """A free-text source would let a caller fabricate how consent was given."""
    user = make_user(db, "Kavya", UserRole.CUSTOMER, phone="+919111100003")

    res = client.post("/auth/whatsapp-consent",
                      json={"opt_in": True, "source": "imported_from_a_list"},
                      headers=auth(user))

    assert res.status_code == 422


def test_a_customer_can_opt_back_out(client, db):
    user = make_user(db, "Kavya", UserRole.CUSTOMER, phone="+919111100004")
    user.whatsapp_opt_in = True
    db.commit()

    res = client.post("/auth/whatsapp-consent", json={"opt_in": False}, headers=auth(user))

    assert res.status_code == 200
    db.refresh(user)
    assert user.whatsapp_opt_in is False
    assert user.whatsapp_opt_out_at is not None
    assert may_message(user) is False


def test_consent_cannot_be_given_on_somebody_elses_behalf(client, db):
    """
    The endpoint takes no user id, deliberately. This is the test that says so,
    because the obvious "improvement" later is to let an admin set it for a
    customer - which is the one thing consent cannot be.
    """
    user = make_user(db, "Kavya", UserRole.CUSTOMER, phone="+919111100005")
    other = make_user(db, "Someone Else", UserRole.CUSTOMER, phone="+919111100006")
    other.whatsapp_opt_in = False
    db.commit()

    client.post("/auth/whatsapp-consent",
                json={"opt_in": True, "user_id": other.id, "source": "checkout"},
                headers=auth(user))

    db.refresh(other)
    assert other.whatsapp_opt_in is False, "one account opted another one in"


def test_consent_needs_a_login(client, db):
    res = client.post("/auth/whatsapp-consent", json={"opt_in": True})
    assert res.status_code in (401, 403)


# ─── THE SHOP CAN SEE WHO IS UNREACHABLE ──────────────

def test_the_profile_says_whether_updates_are_on(client, db):
    user = make_user(db, "Kavya", UserRole.CUSTOMER, phone="+919111100007")
    user.whatsapp_opt_in = False
    db.commit()

    res = client.get("/auth/me", headers=auth(user))

    assert res.status_code == 200
    assert res.json()["whatsapp_opt_in"] is False


def test_an_order_says_whether_its_customer_will_hear_anything(client, db, admin):
    """
    Without this the admin table shows an order that looks perfectly healthy
    while every message about it is being dropped.
    """
    user = make_user(db, "Kavya", UserRole.CUSTOMER, phone="+919111100008")
    user.whatsapp_opt_in = False
    db.commit()
    order = make_order(db, user, status=OrderStatus.CONFIRMED)

    res = client.get(f"/orders/{order.id}", headers=auth(admin))

    assert res.status_code == 200, res.text
    assert res.json()["customer_wa_opt_in"] is False


def test_an_order_for_a_reachable_customer_says_so(client, db, admin):
    user = make_user(db, "Priya", UserRole.CUSTOMER, phone="+919111100009")
    user.whatsapp_opt_in = True
    db.commit()
    order = make_order(db, user, status=OrderStatus.CONFIRMED)

    res = client.get(f"/orders/{order.id}", headers=auth(admin))

    assert res.json()["customer_wa_opt_in"] is True
