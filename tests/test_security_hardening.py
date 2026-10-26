"""
Security hardening pinned by this pass.

Each test fails against the code as it was before the pass:

  * per-IP rate limits were keyed on Railway's proxy, so they never tripped;
  * the OTP had no per-handset guess limit, so a six-digit code was brute
    forceable, and the verify code reached the provider URL unvalidated;
  * the half-authenticated otp_pending token opened the tracking sockets;
  * /orders/track let any baker or rider read any order;
  * uploads took their saved extension from the filename, and the public
    enquiry endpoints were unthrottled.
"""

import pytest
from starlette.websockets import WebSocketDisconnect

from app.core import ratelimit
from app.core.auth import create_access_token, session_payload
from app.models.order import OrderStatus
from app.services import otp_service

from tests.conftest import auth, make_order, make_user, token_for
from app.models.user import UserRole


# ─── RATE LIMIT KEY ───────────────────────────────────

class _Req:
    def __init__(self, headers, peer=None):
        self.headers = headers
        self.client = type("C", (), {"host": peer})() if peer else None


def test_client_ip_prefers_the_envoy_header():
    """Railway's Envoy sets the true client and a caller cannot forge it."""
    r = _Req({
        "x-envoy-external-address": "49.36.1.1",
        # Even a plausible XFF must not win over Envoy's value.
        "x-forwarded-for": "8.8.8.8, 100.64.0.6",
    }, peer="100.64.0.18")
    assert ratelimit.client_ip(r) == "49.36.1.1"


def test_forged_xff_cannot_choose_the_key():
    """A client-supplied X-Forwarded-For must not become the rate-limit bucket."""
    # No Envoy header; the attacker prepends a value they vary per request. We
    # fall back to the real TCP peer instead of trusting any XFF entry.
    r = _Req({"x-forwarded-for": "1.2.3.4, 49.36.1.1"}, peer="203.0.113.9")
    assert ratelimit.client_ip(r) == "203.0.113.9"


def test_client_ip_falls_back_to_the_tcp_peer_without_envoy():
    """Outside Railway there is no Envoy header; key on the unspoofable peer."""
    r = _Req({}, peer="198.51.100.5")
    assert ratelimit.client_ip(r) == "198.51.100.5"


def test_login_rate_limit_actually_trips(client, db):
    make_user(db, "Rate Target", UserRole.CUSTOMER, phone="+919800000001")
    codes = [
        client.post("/auth/login",
                    json={"phone": "+919800000001", "password": "wrong"}).status_code
        for _ in range(7)
    ]
    assert 429 in codes, codes


# ─── OTP BRUTE FORCE ──────────────────────────────────

@pytest.fixture
def otp_redis(monkeypatch, fake_redis):
    monkeypatch.setattr(otp_service, "_get_redis", lambda: fake_redis)
    return fake_redis


@pytest.fixture
def test_mode(monkeypatch):
    monkeypatch.setattr(otp_service, "_test_mode", lambda: True)


def test_a_code_is_burned_after_five_wrong_guesses(test_mode, otp_redis):
    phone = "+919222222222"
    code = otp_service.send_otp(phone)["otp"]
    for _ in range(otp_service.MAX_VERIFY_ATTEMPTS):
        assert otp_service.verify_otp(phone, "000001") is False
    # The real code no longer works — the guesser has spent the code, not just
    # this attempt.
    assert otp_service.verify_otp(phone, code) is False


def test_sends_are_capped_per_handset(test_mode, otp_redis):
    phone = "+919222222222"
    sent = [otp_service.send_otp(phone).get("sent") for _ in range(7)]
    assert sent[: otp_service.MAX_SENDS_PER_WINDOW] == [True] * otp_service.MAX_SENDS_PER_WINDOW
    assert sent[otp_service.MAX_SENDS_PER_WINDOW] is False


def test_send_cap_is_per_handset_however_the_number_is_spelled(test_mode, otp_redis):
    for _ in range(otp_service.MAX_SENDS_PER_WINDOW):
        otp_service.send_otp("+919222222222")
    # Same handset, different spelling — must share the counter, not reset it.
    assert otp_service.send_otp("919222222222").get("sent") is False


@pytest.mark.parametrize("bad", ["", "abcd", "12", "12345678901", "1 2 3", "0x1234", None])
def test_a_malformed_code_never_verifies(test_mode, otp_redis, bad):
    assert otp_service.verify_otp("+919222222222", bad) is False


# ─── otp_pending TOKEN IS NOT A SESSION ───────────────

def test_session_payload_rejects_an_otp_pending_token():
    pending = create_access_token(1, "otp_pending", expires_minutes=10)
    assert session_payload(pending) is None
    real = create_access_token(1, "customer")
    assert session_payload(real) is not None


def test_tracking_socket_refuses_an_otp_pending_token(client, db, customer, rider):
    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY, rider=rider)
    pending = create_access_token(customer.id, "otp_pending", expires_minutes=10)
    with pytest.raises(WebSocketDisconnect):
        with client.websocket_connect(f"/ws/track/{order.id}?token={pending}") as ws:
            ws.receive_text()


# ─── /orders/track ACCESS ─────────────────────────────

def test_unrelated_rider_cannot_track_a_strangers_order(client, db, customer, rider):
    other_rider = make_user(db, "Idle Rider", UserRole.RIDER)
    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY, rider=rider)
    res = client.get(f"/orders/track/{order.id}", headers=auth(other_rider))
    assert res.status_code == 403


def test_assigned_rider_can_track_their_order(client, db, customer, rider):
    order = make_order(db, customer, status=OrderStatus.OUT_FOR_DELIVERY, rider=rider)
    res = client.get(f"/orders/track/{order.id}", headers=auth(rider))
    assert res.status_code == 200


def test_owner_can_track_their_order(client, db, customer):
    order = make_order(db, customer, status=OrderStatus.CONFIRMED)
    res = client.get(f"/orders/track/{order.id}", headers=auth(customer))
    assert res.status_code == 200


# ─── BODY SIZE CAP ────────────────────────────────────

def _sized_client(max_bytes):
    from fastapi import FastAPI, Request
    from fastapi.testclient import TestClient
    from app.core.request_limits import BodySizeLimitMiddleware

    app = FastAPI()
    app.add_middleware(BodySizeLimitMiddleware, max_bytes=max_bytes)

    @app.post("/echo")
    async def echo(request: Request):
        body = await request.body()
        return {"len": len(body)}

    return TestClient(app)


def test_a_declared_oversize_body_is_refused_before_the_route():
    client = _sized_client(100)
    res = client.post("/echo", content=b"x" * 500)
    assert res.status_code == 413


def test_a_body_within_the_cap_passes():
    client = _sized_client(100)
    res = client.post("/echo", content=b"x" * 50)
    assert res.status_code == 200
    assert res.json() == {"len": 50}


# ─── /media SERVES IMAGES WITH THE RIGHT TYPE ─────────
#
# nosniff on /media is only safe if the content-type is correct at the source.
# StaticFiles reads it from the OS mimetypes table, which has no webp entry on
# the slim image, so a .webp would go out as text/plain and — with nosniff —
# not render. main.py registers the type; this proves it stuck.

def _put_media_file(name, head):
    import os
    from app.config import get_settings
    d = get_settings().UPLOAD_DIR
    os.makedirs(d, exist_ok=True)
    with open(os.path.join(d, name), "wb") as f:
        f.write(head + bytes(32))
    return name


def test_media_serves_webp_as_an_image_not_text(client):
    name = _put_media_file("probe.webp", b"RIFF\x00\x00\x00\x00WEBP")
    res = client.get(f"/media/{name}")
    assert res.status_code == 200
    assert res.headers["content-type"].startswith("image/webp")
    assert res.headers.get("x-content-type-options") == "nosniff"


def test_media_responses_are_sandboxed(client):
    name = _put_media_file("probe2.png", bytes([0x89]) + b"PNG")
    res = client.get(f"/media/{name}")
    assert "sandbox" in res.headers.get("content-security-policy", "")


# ─── PUBLIC ENQUIRY THROTTLE ──────────────────────────

def test_enquiry_creation_is_rate_limited(client, db):
    codes = [
        client.post("/build-a-cake/enquiries",
                    json={"contact_name": "A", "contact_phone": "9876543210"}).status_code
        for _ in range(8)
    ]
    assert 429 in codes, codes
