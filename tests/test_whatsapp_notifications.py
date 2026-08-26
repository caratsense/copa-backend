"""
Outbound WhatsApp: who gets told what, and what happens when Meta misbehaves.

The governing rule throughout: a notification problem must never corrupt or
roll back an order transition that has already been committed.
"""

import pytest

from app.config import get_settings
from app.models.order import OrderStatus
from app.models.user import UserRole
from app.models.whatsapp_message import WhatsAppMessage, WhatsAppMessageStatus
from app.services import wa_outbox, wa_templates

from tests.conftest import auth, make_order, make_user

settings = get_settings()


def templates_sent(db, order_id=None):
    q = db.query(WhatsAppMessage)
    if order_id:
        q = q.filter(WhatsAppMessage.order_id == order_id)
    return q.order_by(WhatsAppMessage.id.asc()).all()


def keys_sent(db, order_id=None, status=WhatsAppMessageStatus.SENT):
    return [m.template_key for m in templates_sent(db, order_id) if m.status == status]


@pytest.fixture
def staffed(db):
    """An admin, a baker, a rider and a customer, all opted in."""
    def opted(name, role, phone):
        u = make_user(db, name, role, phone=phone)
        u.whatsapp_opt_in = True
        db.commit()
        return u

    return {
        "admin": opted("Shriya", UserRole.ADMIN, "+919554444462"),
        "baker": opted("Baker", UserRole.BAKER, "+919111111111"),
        "rider": opted("Rider", UserRole.RIDER, "+919333333333"),
        "customer": opted("Priya", UserRole.CUSTOMER, "+919222222222"),
    }


# ─── 21-23. THE RIGHT PERSON GETS THE RIGHT MESSAGE ──

def test_assignment_notifies_the_baker(client, db, wa_secret, staffed):
    """
    Assignment used to write order.status directly, so the baker template
    existed but could never fire.
    """
    order = make_order(db, staffed["customer"], OrderStatus.CONFIRMED)
    res = client.post(f"/admin/orders/{order.id}/assign-baker",
                      headers=auth(staffed["admin"]),
                      json={"staff_id": staffed["baker"].id})
    assert res.status_code == 200

    sent = [m for m in templates_sent(db, order.id) if m.template_key == "baker_new_order"]
    assert len(sent) == 1
    assert sent[0].recipient.endswith("9111111111")
    assert sent[0].status == WhatsAppMessageStatus.SENT


def test_admin_assign_buttons_accept_an_empty_body(client, db, wa_secret, staffed):
    """The admin UI posts `{}`; this returned 422 and both buttons were dead."""
    order = make_order(db, staffed["customer"], OrderStatus.CONFIRMED)
    res = client.post(f"/admin/orders/{order.id}/assign-baker",
                      headers=auth(staffed["admin"]), json={})
    assert res.status_code == 200, res.text


def test_out_for_delivery_notifies_the_customer(client, db, wa_secret, staffed):
    """The proposal promised this and the dispatcher had no branch for it."""
    order = make_order(db, staffed["customer"], OrderStatus.PACKAGED, staffed["rider"])
    res = client.post(f"/orders/{order.id}/rider-pickup", headers=auth(staffed["rider"]))
    assert res.status_code == 200

    assert "order_out_for_delivery" in keys_sent(db, order.id)
    row = next(m for m in templates_sent(db, order.id)
               if m.template_key == "order_out_for_delivery")
    assert row.recipient.endswith("9222222222"), "went to the wrong number"


def test_baker_starting_work_is_not_told_to_redo_it(client, db, wa_secret, staffed):
    """IN_PRODUCTION fired the rework template on both routes into it."""
    order = make_order(db, staffed["customer"], OrderStatus.ASSIGNED, baker=staffed["baker"])
    res = client.post(f"/baker/orders/{order.id}/start-baking", headers=auth(staffed["baker"]))
    assert res.status_code == 200

    assert "order_rework" not in keys_sent(db, order.id)


def test_quality_check_rejection_does_send_rework(client, db, wa_secret, staffed):
    order = make_order(db, staffed["customer"], OrderStatus.AWAITING_APPROVAL,
                       baker=staffed["baker"])
    from app.schemas import StatusUpdate
    from app.services.order_service import update_order_status

    update_order_status(db, order.id, StatusUpdate(status="IN_PRODUCTION"), rework=True)
    assert "order_rework" in keys_sent(db, order.id)


def test_rider_assigned_after_packaging_is_notified(client, db, wa_secret, staffed):
    """No PACKAGED transition is coming, so nothing used to tell them."""
    order = make_order(db, staffed["customer"], OrderStatus.PACKAGED)
    res = client.post(f"/admin/orders/{order.id}/assign-rider",
                      headers=auth(staffed["admin"]),
                      json={"staff_id": staffed["rider"].id})
    assert res.status_code == 200

    assert "rider_new_delivery" in keys_sent(db, order.id)


def test_rider_is_not_notified_twice_on_the_packaged_transition(client, db, wa_secret, staffed):
    order = make_order(db, staffed["customer"], OrderStatus.AWAITING_APPROVAL,
                       rider=staffed["rider"], baker=staffed["baker"])
    from app.schemas import StatusUpdate
    from app.services.order_service import update_order_status

    update_order_status(db, order.id, StatusUpdate(status="PACKAGED"))
    assert keys_sent(db, order.id).count("rider_new_delivery") == 1


def test_no_message_reaches_a_recipient_without_opt_in(client, db, wa_secret, staffed):
    staffed["customer"].whatsapp_opt_in = False
    db.commit()

    order = make_order(db, staffed["customer"], OrderStatus.PACKAGED, staffed["rider"])
    client.post(f"/orders/{order.id}/rider-pickup", headers=auth(staffed["rider"]))

    row = next(m for m in templates_sent(db, order.id)
               if m.template_key == "order_out_for_delivery")
    assert row.status == WhatsAppMessageStatus.SKIPPED
    assert "opt-in" in (row.last_error or "")


def test_delivered_notifies_customer_and_admin(client, db, wa_secret, staffed):
    order = make_order(db, staffed["customer"], OrderStatus.OUT_FOR_DELIVERY, staffed["rider"])
    client.post(f"/orders/{order.id}/rider-delivered", headers=auth(staffed["rider"]))

    keys = keys_sent(db, order.id)
    assert "order_delivered" in keys
    assert "order_delivered_admin" in keys


# ─── 24-28. FAILURE HANDLING ─────────────────────────

def test_meta_failure_does_not_corrupt_the_order_status(client, db, wa_secret, staffed, monkeypatch):
    from app.services import whatsapp_sender

    monkeypatch.setattr(whatsapp_sender, "_send",
                        lambda payload: {"error": {"message": "Template does not exist", "code": 132001}})

    order = make_order(db, staffed["customer"], OrderStatus.PACKAGED, staffed["rider"])
    res = client.post(f"/orders/{order.id}/rider-pickup", headers=auth(staffed["rider"]))

    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY, "a Meta error rolled back a real transition"

    failed = [m for m in templates_sent(db, order.id) if m.status == WhatsAppMessageStatus.FAILED]
    assert failed, "failure was not recorded anywhere"
    assert "132001" in (failed[0].last_error or "") or "Template" in (failed[0].last_error or "")


def test_network_timeout_does_not_corrupt_the_order_status(client, db, wa_secret, staffed, monkeypatch):
    import httpx
    from app.services import whatsapp_sender

    def boom(payload):
        raise httpx.ReadTimeout("Meta timed out")

    monkeypatch.setattr(whatsapp_sender, "_send", boom)

    order = make_order(db, staffed["customer"], OrderStatus.PACKAGED, staffed["rider"])
    res = client.post(f"/orders/{order.id}/rider-pickup", headers=auth(staffed["rider"]))

    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY


def test_failed_notifications_can_be_retried(client, db, wa_secret, staffed, monkeypatch):
    from app.services import whatsapp_sender

    monkeypatch.setattr(whatsapp_sender, "_send", lambda p: {"error": {"message": "transient"}})
    order = make_order(db, staffed["customer"], OrderStatus.PACKAGED, staffed["rider"])
    client.post(f"/orders/{order.id}/rider-pickup", headers=auth(staffed["rider"]))
    assert any(m.status == WhatsAppMessageStatus.FAILED for m in templates_sent(db, order.id))

    # Meta recovers.
    monkeypatch.setattr(whatsapp_sender, "_send",
                        lambda p: {"messages": [{"id": "wamid.RETRY"}]})
    result = wa_outbox.retry_pending(db)

    assert result["sent"] >= 1
    assert "order_out_for_delivery" in keys_sent(db, order.id)


def test_retry_gives_up_after_max_attempts(client, db, wa_secret, staffed, monkeypatch):
    from app.services import whatsapp_sender

    monkeypatch.setattr(whatsapp_sender, "_send", lambda p: {"error": {"message": "permanent"}})
    order = make_order(db, staffed["customer"], OrderStatus.PACKAGED, staffed["rider"])
    client.post(f"/orders/{order.id}/rider-pickup", headers=auth(staffed["rider"]))

    for _ in range(6):
        wa_outbox.retry_pending(db)

    rows = [m for m in templates_sent(db, order.id) if m.template_key == "order_out_for_delivery"]
    assert rows and rows[0].attempts <= wa_outbox.MAX_ATTEMPTS, "retried past the cap"


def test_missing_template_configuration_fails_cleanly(db):
    with pytest.raises(KeyError) as exc:
        wa_templates.get("no_such_template")
    assert "Known keys" in str(exc.value)


def test_invalid_phone_number_is_recorded_not_raised(client, db, wa_secret, staffed):
    staffed["customer"].phone = ""
    db.commit()

    order = make_order(db, staffed["customer"], OrderStatus.PACKAGED, staffed["rider"])
    res = client.post(f"/orders/{order.id}/rider-pickup", headers=auth(staffed["rider"]))
    assert res.status_code == 200

    row = next(m for m in templates_sent(db, order.id)
               if m.template_key == "order_out_for_delivery")
    assert row.status == WhatsAppMessageStatus.SKIPPED
    assert "phone" in (row.last_error or "")


# ─── TEMPLATE REGISTRY ───────────────────────────────

def test_every_template_key_resolves_to_a_meta_name():
    for key, tpl in wa_templates.TEMPLATES.items():
        assert tpl.meta_name, f"{key} has no Meta name"
        assert tpl.language, f"{key} has no language"
        assert tpl.recipient and tpl.trigger


def test_template_names_are_overridable_without_a_code_change(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_NAMES",
                        '{"order_delivered": {"name": "coc_delivered_v3", "language": "en_US"}}')
    wa_templates._reset_cache_for_tests()

    tpl = wa_templates.get("order_delivered")
    assert tpl.meta_name == "coc_delivered_v3"
    assert tpl.language == "en_US"
    # Untouched keys keep their defaults.
    assert wa_templates.get("order_confirmation").meta_name == "order_confirmation"


def test_malformed_template_override_falls_back_to_defaults(monkeypatch):
    monkeypatch.setattr(settings, "WHATSAPP_TEMPLATE_NAMES", "{not valid json")
    wa_templates._reset_cache_for_tests()
    assert wa_templates.get("order_delivered").meta_name == "order_delivered"


def test_handover_table_lists_every_template():
    rows = wa_templates.describe_all()
    assert len(rows) == len(wa_templates.TEMPLATES)
    for row in rows:
        assert row["body_placeholders"] == len(row["variables"])
