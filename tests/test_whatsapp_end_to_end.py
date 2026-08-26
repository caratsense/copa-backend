"""
End-to-end operational chain, driven the way the business actually runs it.

    customer order -> baker assigned -> baker WhatsApp -> baker replies
    -> quality check -> admin approves -> rider assigned -> rider WhatsApp
    -> out for delivery (+ customer told) -> live tracking -> delivered
    -> customer told -> tracking cleaned up

Half the transitions are driven from WhatsApp and half from the website, on the
same order, precisely to prove the two cannot drift apart. No real Meta call is
made — the sender is mocked at the HTTP boundary.
"""

from app.models.order import OrderStatus
from app.models.user import UserRole
from app.models.whatsapp_message import WhatsAppMessage, WhatsAppMessageStatus
from app.services.delivery_tracking import get_rider_location, update_rider_location

from tests.conftest import auth, make_order, make_user
from tests.test_whatsapp_webhook import message, post


def _optin(db, user):
    user.whatsapp_opt_in = True
    db.commit()
    return user


def _keys(db, order_id):
    return [
        m.template_key
        for m in db.query(WhatsAppMessage)
        .filter(WhatsAppMessage.order_id == order_id)
        .order_by(WhatsAppMessage.id.asc())
        .all()
        if m.status == WhatsAppMessageStatus.SENT
    ]


def test_full_operational_chain_website_and_whatsapp_agree(client, db, wa_secret, fake_redis):
    admin = _optin(db, make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462"))
    baker = _optin(db, make_user(db, "Rahul Baker", UserRole.BAKER, phone="+919111111111"))
    rider = _optin(db, make_user(db, "Aman Rider", UserRole.RIDER, phone="+919333333333"))
    customer = _optin(db, make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222"))

    # ── 1. Order exists and a baker is assigned (website) ──
    order = make_order(db, customer, OrderStatus.CONFIRMED)
    res = client.post(f"/admin/orders/{order.id}/assign-baker",
                      headers=auth(admin), json={"staff_id": baker.id})
    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.ASSIGNED
    assert order.assigned_baker_id == baker.id
    assert "baker_new_order" in _keys(db, order.id), "baker was never told"

    # ── 2. Baker starts, from WhatsApp ──
    assert post(client, message(f"START {order.id}", "919111111111",
                                msg_id="wamid.E1")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.IN_PRODUCTION
    assert "order_rework" not in _keys(db, order.id), "starting work looked like a rejection"

    # ── 3. Baker replies "Completed" with no order number ──
    assert post(client, message("Completed", "919111111111",
                                msg_id="wamid.E2")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL
    assert "admin_approval_needed" in _keys(db, order.id), "Shriya was not asked to QC"

    # ── 4. Shriya sees it in the admin queue ──
    listed = client.get("/dashboard/orders", headers=auth(admin),
                        params={"status": "AWAITING_APPROVAL"}).json()
    assert any(o["id"] == order.id for o in listed)

    # ── 5. QC passes, from WhatsApp ──
    assert post(client, message(f"APPROVE {order.id}", "919554444462",
                                msg_id="wamid.E3")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.PACKAGED

    # ── 6. A rider is assigned and told ──
    if order.assigned_rider_id is None:
        client.post(f"/admin/orders/{order.id}/assign-rider",
                    headers=auth(admin), json={"staff_id": rider.id})
        db.refresh(order)
    assert order.assigned_rider_id is not None
    assert "rider_new_delivery" in _keys(db, order.id), "rider was never told"

    # ── 7. Rider collects, from WhatsApp; customer is told ──
    rider_phone = "919333333333" if order.assigned_rider_id == rider.id else None
    if rider_phone:
        assert post(client, message(f"PICKED {order.id}", rider_phone,
                                    msg_id="wamid.E4")).status_code == 200
    else:
        client.post(f"/orders/{order.id}/rider-pickup",
                    headers=auth(db.query(type(rider)).get(order.assigned_rider_id)))
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY
    assert "order_out_for_delivery" in _keys(db, order.id), "customer not told it was on its way"

    # ── 8. Live tracking works exactly as it does for a website-driven order ──
    update_rider_location(order.id, 26.8467, 80.9462)
    assert get_rider_location(order.id)["rider_lat"] == 26.8467
    fleet = client.get("/delivery/admin/active", headers=auth(admin)).json()
    assert any(d["order_id"] == order.id for d in fleet["deliveries"])

    # ── 9. Delivered, from WhatsApp ──
    if rider_phone:
        assert post(client, message(f"DELIVERED {order.id}", rider_phone,
                                    msg_id="wamid.E5")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERED

    # ── 10. Customer told, tracking cleaned up, fleet view emptied ──
    keys = _keys(db, order.id)
    assert "order_delivered" in keys
    assert fake_redis.exists(f"delivery:{order.id}") == 0, "tracking state survived delivery"
    fleet = client.get("/delivery/admin/active", headers=auth(admin)).json()
    assert not any(d["order_id"] == order.id for d in fleet["deliveries"])

    # ── 11. The whole chain notified the right people, once each ──
    assert keys.count("baker_new_order") == 1
    assert keys.count("order_out_for_delivery") == 1
    assert keys.count("order_delivered") == 1


def test_whatsapp_and_website_produce_identical_side_effects(client, db, wa_secret, fake_redis):
    """
    Same transition, two entry points. If these ever diverge, the WhatsApp bot
    has become a second order system.
    """
    admin = _optin(db, make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462"))
    rider = _optin(db, make_user(db, "Rider", UserRole.RIDER, phone="+919333333333"))
    c1 = _optin(db, make_user(db, "C1", UserRole.CUSTOMER, phone="+919222222221"))
    c2 = _optin(db, make_user(db, "C2", UserRole.CUSTOMER, phone="+919222222222"))

    via_web = make_order(db, c1, OrderStatus.PACKAGED, rider)
    via_wa = make_order(db, c2, OrderStatus.PACKAGED, rider)

    client.post(f"/orders/{via_web.id}/rider-pickup", headers=auth(rider))
    post(client, message(f"PICKED {via_wa.id}", "919333333333", msg_id="wamid.CMP"))

    db.refresh(via_web); db.refresh(via_wa)
    assert via_web.status == via_wa.status == OrderStatus.OUT_FOR_DELIVERY
    assert _keys(db, via_web.id) == _keys(db, via_wa.id)

    # Both are tracked identically by the fleet view.
    update_rider_location(via_web.id, 26.85, 80.95)
    update_rider_location(via_wa.id, 26.85, 80.95)
    fleet = client.get("/delivery/admin/active", headers=auth(admin)).json()
    states = {
        d["order_id"]: d["tracking_state"]
        for d in fleet["deliveries"] if d["order_id"] in (via_web.id, via_wa.id)
    }
    assert states[via_web.id] == states[via_wa.id] == "live"
