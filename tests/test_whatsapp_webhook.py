"""
WhatsApp inbound webhook: authenticity, parsing, idempotency and command safety.

No real Meta call is made anywhere here — `sent_messages` captures every
outbound payload at the HTTP boundary.
"""

import hashlib
import hmac
import json

import pytest

from app.config import get_settings
from app.models.order import OrderStatus
from app.services import wa_commands
from app.models.user import UserRole

from tests.conftest import make_order, make_user

settings = get_settings()
URL = "/webhook/whatsapp"


def sign(body: bytes, secret: str) -> str:
    return "sha256=" + hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()


def post(client, payload: dict, secret: str | None = "test-app-secret", raw: bytes | None = None):
    body = raw if raw is not None else json.dumps(payload).encode()
    headers = {"Content-Type": "application/json"}
    if secret:
        headers["x-hub-signature-256"] = sign(body, secret)
    return client.post(URL, content=body, headers=headers)


def message(text: str, sender: str, msg_id: str = "wamid.TEST1", msg_type: str = "text") -> dict:
    inner = {"id": msg_id, "from": sender, "type": msg_type}
    if msg_type == "text":
        inner["text"] = {"body": text}
    return {"entry": [{"changes": [{"value": {"messages": [inner]}}]}]}


# ─── 1-4. VERIFICATION AND AUTHENTICITY ──────────────

def test_get_verification_succeeds_with_correct_token(client, wa_secret):
    res = client.get(URL, params={
        "hub.mode": "subscribe",
        "hub.verify_token": settings.WHATSAPP_WEBHOOK_VERIFY_TOKEN,
        "hub.challenge": "challenge-123",
    })
    assert res.status_code == 200
    assert res.text == "challenge-123"


def test_get_verification_fails_with_wrong_token(client, wa_secret):
    res = client.get(URL, params={
        "hub.mode": "subscribe",
        "hub.verify_token": "wrong-token",
        "hub.challenge": "challenge-123",
    })
    assert res.status_code == 403


def test_post_with_invalid_signature_is_rejected(client, wa_secret):
    body = json.dumps(message("QUEUE", "919000000001")).encode()
    res = client.post(URL, content=body, headers={
        "x-hub-signature-256": "sha256=" + "0" * 64,
    })
    assert res.status_code == 403


def test_post_with_no_signature_is_rejected(client, wa_secret):
    res = client.post(URL, json=message("QUEUE", "919000000001"))
    assert res.status_code == 403


def test_post_with_valid_signature_is_accepted(client, wa_secret):
    res = post(client, message("QUEUE", "919000000001"))
    assert res.status_code == 200


def test_webhook_fails_closed_when_app_secret_is_unset(client, monkeypatch):
    """
    The production posture that made this endpoint a remote order-management
    API for anyone who knew the URL.
    """
    monkeypatch.setattr(settings, "WHATSAPP_APP_SECRET", "")
    res = client.post(URL, json=message("APPROVE 1", "919000000001"))
    assert res.status_code == 403


# ─── 5-7. IDEMPOTENCY AND MALFORMED PAYLOADS ─────────

def test_duplicate_meta_message_id_is_processed_once(client, db, wa_secret, sent_messages):
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)

    payload = message(f"DONE {order.id}", "919111111111", msg_id="wamid.DUP")
    assert post(client, payload).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL

    # Meta redelivers the same id — must not advance again.
    assert post(client, payload).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL


@pytest.mark.parametrize("payload", [
    {},
    {"entry": []},
    {"entry": [{}]},
    {"entry": [{"changes": []}]},
    {"entry": [{"changes": [{}]}]},
    {"entry": [{"changes": [{"value": None}]}]},
    {"entry": [{"changes": [{"value": {"messages": None}}]}]},
    # `x.get(k, {})` returns None for an explicit null — this used to 500.
    {"entry": [{"changes": [{"value": {"messages": [{"id": "m", "from": "91900", "type": "text", "text": None}]}}]}]},
    {"entry": [{"changes": [{"value": {"messages": [{"id": "m", "from": "91900", "type": "interactive", "interactive": None}]}}]}]},
    {"entry": ["not-a-dict"]},
    {"entry": [{"changes": ["not-a-dict"]}]},
])
def test_malformed_payloads_never_500(client, wa_secret, payload):
    """A non-2xx makes Meta retry the whole batch — a parse bug becomes a loop."""
    res = post(client, payload)
    assert res.status_code == 200, res.text


def test_top_level_non_object_body_is_handled(client, wa_secret):
    assert post(client, None, raw=b"[]").status_code == 200
    assert post(client, None, raw=b"not json at all").status_code == 200


def test_unknown_sender_is_ignored_safely(client, wa_secret, sent_messages):
    res = post(client, message("DONE 1", "919999999999"))
    assert res.status_code == 200


def test_status_callbacks_are_accepted(client, wa_secret):
    payload = {"entry": [{"changes": [{"value": {"statuses": [
        {"id": "wamid.X", "status": "failed", "recipient_id": "91900",
         "errors": [{"code": 132001, "title": "Template does not exist"}]},
    ]}}]}]}
    assert post(client, payload).status_code == 200


def test_every_message_in_a_batch_is_processed(client, db, wa_secret):
    """Only messages[0] used to be read; the rest vanished with a 200."""
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    a = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)
    b = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)

    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.B1", "from": "919111111111", "type": "text", "text": {"body": f"DONE {a.id}"}},
        {"id": "wamid.B2", "from": "919111111111", "type": "text", "text": {"body": f"DONE {b.id}"}},
    ]}}]}]}
    assert post(client, payload).status_code == 200

    db.refresh(a); db.refresh(b)
    assert a.status == OrderStatus.AWAITING_APPROVAL
    assert b.status == OrderStatus.AWAITING_APPROVAL


# ─── SECURITY: PHONE RESOLUTION ──────────────────────

def test_like_wildcard_sender_cannot_impersonate_an_admin(client, db, wa_secret):
    """
    SQLAlchemy's endswith builds an unescaped LIKE pattern. Ten underscores
    matched the first user in the table — an admin — handing full command
    rights to anyone able to reach the webhook.
    """
    admin = make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    order = make_order(db, customer, OrderStatus.AWAITING_APPROVAL, baker=baker)

    res = post(client, message(f"APPROVE {order.id}", "_" * 10))
    assert res.status_code == 200

    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL, "wildcard sender acted as admin"


def test_ambiguous_phone_suffix_refuses_to_guess(client, db, wa_secret):
    """Two accounts sharing the last 10 digits must not resolve to either."""
    make_user(db, "IN Baker", UserRole.BAKER, phone="+919111111111")
    make_user(db, "UK Baker", UserRole.BAKER, phone="+449111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.IN_PRODUCTION,
                       baker=db_query_first_baker(db))

    res = post(client, message(f"DONE {order.id}", "779111111111"))
    assert res.status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.IN_PRODUCTION


def db_query_first_baker(db):
    from app.models.user import User
    return db.query(User).filter(User.role == UserRole.BAKER).first()


# ─── 8-14. BAKER FLOW ────────────────────────────────

def test_correct_baker_completion_updates_the_right_order(client, db, wa_secret):
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    mine = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)
    other = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)

    assert post(client, message(f"DONE {mine.id}", "919111111111")).status_code == 200

    db.refresh(mine); db.refresh(other)
    assert mine.status == OrderStatus.AWAITING_APPROVAL
    assert other.status == OrderStatus.IN_PRODUCTION


def test_wrong_baker_cannot_complete_another_bakers_order(client, db, wa_secret):
    owner = make_user(db, "Owner", UserRole.BAKER, phone="+919111111111")
    intruder = make_user(db, "Intruder", UserRole.BAKER, phone="+919222222222")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=owner)

    assert post(client, message(f"DONE {order.id}", "919222222222")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.IN_PRODUCTION


def test_bare_completed_with_exactly_one_order_completes_it(client, db, wa_secret):
    """The business flow: the baker just replies 'Completed'."""
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)

    assert post(client, message("Completed", "919111111111")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL


def test_bare_completed_with_two_orders_refuses_and_asks(client, db, wa_secret, sent_messages):
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    a = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)
    b = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)

    assert post(client, message("Completed", "919111111111")).status_code == 200

    db.refresh(a); db.refresh(b)
    assert a.status == OrderStatus.IN_PRODUCTION, "guessed an order it should not have"
    assert b.status == OrderStatus.IN_PRODUCTION

    reply = " ".join(m["text"]["body"] for m in sent_messages if m.get("type") == "text")
    assert f"#{a.id}" in reply and f"#{b.id}" in reply


def test_bare_completed_with_no_orders_explains(client, db, wa_secret, sent_messages):
    make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    assert post(client, message("Completed", "919111111111")).status_code == 200
    reply = " ".join(m["text"]["body"] for m in sent_messages if m.get("type") == "text")
    assert "no order" in reply.lower()


def test_completion_moves_order_to_quality_check_state(client, db, wa_secret):
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)

    post(client, message(f"DONE {order.id}", "919111111111"))
    db.refresh(order)
    assert order.status == OrderStatus.AWAITING_APPROVAL


def test_baker_cannot_complete_an_order_at_the_wrong_stage(client, db, wa_secret):
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.ASSIGNED, baker=baker)

    assert post(client, message(f"DONE {order.id}", "919111111111")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.ASSIGNED


# ─── 15-20. QC AND RIDER ─────────────────────────────

def test_admin_approval_packages_the_order(client, db, wa_secret):
    make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462")
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.AWAITING_APPROVAL, baker=baker)

    assert post(client, message(f"APPROVE {order.id}", "919554444462")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.PACKAGED


def test_admin_reject_sends_back_to_production(client, db, wa_secret):
    make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462")
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.AWAITING_APPROVAL, baker=baker)

    assert post(client, message(f"REJECT {order.id}", "919554444462")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.IN_PRODUCTION


def test_correct_rider_transitions_work(client, db, wa_secret):
    rider = make_user(db, "Rider", UserRole.RIDER, phone="+919333333333")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.PACKAGED, rider)

    assert post(client, message(f"PICKED {order.id}", "919333333333")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.OUT_FOR_DELIVERY

    assert post(client, message(f"DELIVERED {order.id}", "919333333333",
                                msg_id="wamid.D2")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.DELIVERED


def test_wrong_rider_cannot_modify_another_riders_order(client, db, wa_secret):
    owner = make_user(db, "Owner", UserRole.RIDER, phone="+919333333333")
    intruder = make_user(db, "Intruder", UserRole.RIDER, phone="+919444444444")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.PACKAGED, owner)

    assert post(client, message(f"PICKED {order.id}", "919444444444")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.PACKAGED


def test_delivery_tracking_cleanup_still_runs_on_whatsapp_delivery(client, db, wa_secret, fake_redis):
    """WhatsApp must produce the same side effects as the website button."""
    from app.services.delivery_tracking import update_rider_location

    rider = make_user(db, "Rider", UserRole.RIDER, phone="+919333333333")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.OUT_FOR_DELIVERY, rider)
    update_rider_location(order.id, 26.85, 80.95)
    assert fake_redis.exists(f"delivery:{order.id}") == 1

    post(client, message(f"DELIVERED {order.id}", "919333333333"))

    db.refresh(order)
    assert order.status == OrderStatus.DELIVERED
    assert fake_redis.exists(f"delivery:{order.id}") == 0


# ─── DETERMINISTIC COMMAND PARSING ───────────────────

@pytest.mark.parametrize("role,text", [
    (UserRole.ADMIN, "Looks like order 145 is wrong"),   # "LOOKS" contains "OK"
    (UserRole.ADMIN, "this order 145 is not ok"),
    (UserRole.BAKER, "I have not started 145 yet"),
    (UserRole.BAKER, "not done with 145 yet"),
    (UserRole.RIDER, "I have not picked 145"),
])
def test_prose_never_parses_as_a_state_changing_command(role, text):
    """Every one of these previously triggered a real transition."""
    command = wa_commands.parse(text, role)
    assert command is None or command.action.endswith("QUEUE") or command.action.endswith("ORDERS"), \
        f"{text!r} parsed as {command}"


def test_ambiguous_numeric_prose_does_not_parse_at_all():
    """
    'DONE 2 cakes for order 145' used to complete order 2 — the first number
    after the verb won. Only an explicit order-number grammar may transition.
    """
    assert wa_commands.parse("DONE 2 cakes for order 145", UserRole.BAKER) is None
    assert wa_commands.parse("DONE 145 please", UserRole.BAKER) is None


@pytest.mark.parametrize("text,expected_id", [
    ("DONE 145", 145),
    ("DONE #145", 145),
    ("DONE ORDER 145", 145),
    ("DONE ORDER #145", 145),
    ("DONE NO 145", 145),
    ("done 145", 145),
])
def test_accepted_order_id_grammar(text, expected_id):
    command = wa_commands.parse(text, UserRole.BAKER)
    assert command is not None and command.order_id == expected_id


def test_ambiguous_command_reaches_the_baker_as_help_not_a_transition(client, db, wa_secret, sent_messages):
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    a = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)

    assert post(client, message("DONE 2 cakes for order 145", "919111111111")).status_code == 200

    db.refresh(a)
    assert a.status == OrderStatus.IN_PRODUCTION
    reply = " ".join(m["text"]["body"] for m in sent_messages if m.get("type") == "text")
    assert "DONE" in reply and "order no" in reply.lower()


@pytest.mark.parametrize("text,expected", [
    ("DONE 145", ("BAKER_DONE", 145)),
    ("done #145", ("BAKER_DONE", 145)),
    ("Completed 145", ("BAKER_DONE", 145)),
    ("  START   145 ", ("BAKER_START", 145)),
])
def test_valid_baker_commands_parse(text, expected):
    command = wa_commands.parse(text, UserRole.BAKER)
    assert (command.action, command.order_id) == expected


def test_bare_verb_flags_that_it_needs_an_order_id():
    command = wa_commands.parse("Completed", UserRole.BAKER)
    assert command.action == "BAKER_DONE"
    assert command.order_id is None
    assert command.needs_order_id is True


def test_llm_is_not_consulted_for_staff_commands(client, db, wa_secret, monkeypatch):
    """A language model must never be able to author a state transition."""
    import app.services.gemini_parser as gp

    def explode(*a, **k):
        raise AssertionError("LLM was consulted for a staff command")

    monkeypatch.setattr(gp, "parse_message", explode)
    baker = make_user(db, "Baker", UserRole.BAKER, phone="+919111111111")
    customer = make_user(db, "Cust", UserRole.CUSTOMER)
    order = make_order(db, customer, OrderStatus.IN_PRODUCTION, baker=baker)

    assert post(client, message("please finish it up mate", "919111111111")).status_code == 200
    db.refresh(order)
    assert order.status == OrderStatus.IN_PRODUCTION


# ─── 12. THE WORK HAPPENS AFTER THE RESPONSE ─────────
# Handling one message makes up to five blocking HTTP calls - fetching a voice
# note, downloading it, transcribing it, asking the language model, sending the
# reply. This ran inline in an `async def` route, which means it ran on the
# event loop, which the server has exactly one of: a single voice note could
# stall the whole backend - the website too - for over a minute.
#
# TestClient drains background tasks before it returns, so no test that posts
# to the endpoint can tell the two shapes apart. These call the route function
# directly and look at what it did with the message.

class _FakeRequest:
    """A minimal stand-in for the Request the route reads."""

    def __init__(self, body: bytes, secret: str):
        self._body = body
        self.headers = {"x-hub-signature-256": sign(body, secret)}

    async def body(self) -> bytes:
        return self._body


def _signed_request(payload: dict, secret: str = "test-app-secret"):
    return _FakeRequest(json.dumps(payload).encode(), secret)


def test_a_message_is_queued_not_handled_on_the_request_path(wa_secret, monkeypatch):
    """The route must return without having done any of the work."""
    import asyncio
    from fastapi import BackgroundTasks

    from app.api.routes import webhook as wh

    called = []
    monkeypatch.setattr(wh, "_process_message", lambda msg: called.append(msg) or "ok")

    background = BackgroundTasks()
    result = asyncio.run(wh.receive_whatsapp(
        _signed_request(message("QUEUE", "919000000001")), background))

    assert result["status"] == "ok"
    assert called == [], "the message was handled on the event loop"
    assert len(background.tasks) == 1, "the message was not queued either"

    # And the queued task does the work when it is run.
    asyncio.run(background())
    assert len(called) == 1


def test_every_message_in_a_batch_is_queued(wa_secret, monkeypatch):
    """Meta batches. Queuing must not drop the siblings the old loop handled."""
    import asyncio
    from fastapi import BackgroundTasks

    from app.api.routes import webhook as wh

    monkeypatch.setattr(wh, "_process_message", lambda msg: "ok")

    payload = {"entry": [{"changes": [{"value": {"messages": [
        {"id": "wamid.A", "from": "919000000001", "type": "text", "text": {"body": "one"}},
        {"id": "wamid.B", "from": "919000000002", "type": "text", "text": {"body": "two"}},
    ]}}]}]}

    background = BackgroundTasks()
    result = asyncio.run(wh.receive_whatsapp(_signed_request(payload), background))

    assert result["handled"] == 2
    assert len(background.tasks) == 2


def test_one_failing_message_does_not_take_its_siblings_with_it(wa_secret, monkeypatch):
    """
    The old loop caught per message. A background task raises into Starlette
    instead, which would abandon the rest of the batch, so the wrapper has to
    swallow it the same way.
    """
    import asyncio

    from app.api.routes import webhook as wh

    handled = []

    def boom(msg):
        if msg.get("id") == "wamid.A":
            raise RuntimeError("kaboom")
        handled.append(msg["id"])
        return "ok"

    monkeypatch.setattr(wh, "_process_message", boom)

    wh._handle_one_message({"id": "wamid.A"})      # must not raise
    wh._handle_one_message({"id": "wamid.B"})

    assert handled == ["wamid.B"]
