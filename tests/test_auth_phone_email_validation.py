"""
The phone number and email a customer signs up with are validated server-side.

The phone number IS the login identifier and the WhatsApp address, so a bad one
produces an account that cannot be signed into and cannot be messaged. The
frontend checks it, but a form is not a boundary: /auth/register and
/auth/login accepted whatever was posted and stored it raw, so the same
customer could end up in the database as "9876543210" while staff-created rows
were "+919876543210".

Validation therefore lives in the schema, reusing app.core.phone -- the same
normaliser staff creation already used -- so every route that takes a phone
gets it, and none of them has to wonder whether its input is trustworthy.
"""

from app.core.phone import lookup_values, normalize_phone
from app.models.trusted_device import TrustedDevice
from app.models.user import User, UserRole
from app.core.auth import hash_password

from tests.conftest import make_user

import pytest


VALID = "9876543210"
NORMALIZED = "+919876543210"


@pytest.fixture(autouse=True)
def no_rate_limit(monkeypatch):
    """
    Exercise validation, not throttling.

    /auth/register is capped at 10/minute per address and this file posts to it
    far more than that, so without this the later cases fail with 429 and look
    like validation bugs. Scoped to this file: the limits themselves are real
    behaviour and stay switched on everywhere else.
    """
    from app.api.routes import auth as auth_routes
    from app.main import app, limiter as app_limiter

    monkeypatch.setattr(auth_routes.limiter, "enabled", False)
    monkeypatch.setattr(app_limiter, "enabled", False)
    if hasattr(app.state, "limiter"):
        monkeypatch.setattr(app.state.limiter, "enabled", False)
    yield


def _register(client, **overrides):
    body = {"name": "Priya", "phone": VALID, "password": "hunter2hunter2"}
    body.update(overrides)
    return client.post("/auth/register", json=body)


# ─── PHONE: ACCEPTED SHAPES ──────────────────────────

@pytest.mark.parametrize("typed", [
    "9876543210", "+919876543210", "919876543210", "09876543210",
    "+91 98765 43210", "98765-43210", "  9876543210  ",
])
def test_the_shapes_people_actually_type_are_accepted_and_normalized(client, db, typed):
    res = _register(client, phone=typed)

    assert res.status_code == 201, res.text
    assert db.query(User).filter(User.phone == NORMALIZED).count() == 1, \
        f"{typed!r} was not stored as {NORMALIZED}"


@pytest.mark.parametrize("first_digit", ["6", "7", "8", "9"])
def test_every_valid_indian_mobile_prefix_is_accepted(client, db, first_digit):
    res = _register(client, phone=f"{first_digit}876543210")
    assert res.status_code == 201, res.text


# ─── PHONE: REJECTED ─────────────────────────────────

@pytest.mark.parametrize("bad,why", [
    ("5876543210", "starts with 5"),
    ("1234567890", "starts with 1"),
    ("0876543210", "starts with 0 after the trunk strip"),
    ("987654321", "nine digits"),
    ("98765432101", "eleven digits"),
    ("abcdefghij", "letters"),
    ("priya@example.com", "an email in the phone field"),
    ("", "empty"),
    ("          ", "whitespace only"),
    ("+1 415 555 0123", "not an Indian number"),
])
def test_invalid_phone_numbers_are_rejected_with_422(client, db, bad, why):
    res = _register(client, phone=bad)

    assert res.status_code == 422, f"accepted {bad!r} ({why}): {res.status_code}"
    assert db.query(User).count() == 0, f"stored an account for {bad!r}"


def test_the_rejection_says_what_is_wanted(client):
    res = _register(client, phone="5876543210")

    body = str(res.json())
    assert "6, 7, 8 or 9" in body, f"unhelpful error: {body}"


def test_an_email_in_the_phone_field_is_named_as_such(client):
    """The autofill failure that motivated the normaliser in the first place."""
    res = _register(client, phone="priya@example.com")

    assert res.status_code == 422
    assert "email address, not a phone number" in str(res.json())


def test_login_rejects_an_invalid_phone_before_touching_the_database(client, db):
    res = client.post("/auth/login", json={"phone": "abcdefghij", "password": "x"})

    assert res.status_code == 422
    # Not 401: the input never became a credential guess, so it is not an
    # authentication failure and must not burn a rate-limit slot as one.
    assert res.status_code != 401


# ─── PHONE: BYPASS ATTEMPTS ──────────────────────────

@pytest.mark.parametrize("bypass", [
    "9876543210\n",                 # trailing newline
    "9876543210​",             # zero-width space
    "９８７６５４３２１０",            # full-width digits
    "+91+919876543210",             # doubled country code
    "9876543210; DROP TABLE users", # sql-ish
    "<script>9876543210</script>",  # markup
])
def test_bypass_attempts_are_either_normalized_or_rejected(client, db, bypass):
    """
    Whatever happens, the database must not end up holding the raw string.
    Some of these normalise to a valid number, which is fine; none may be
    stored verbatim.
    """
    res = _register(client, phone=bypass)

    assert res.status_code in (201, 422), res.status_code
    assert db.query(User).filter(User.phone == bypass).count() == 0, \
        f"stored {bypass!r} verbatim"
    if res.status_code == 201:
        stored = db.query(User).first().phone
        assert stored.startswith("+91") and len(stored) == 13


def test_a_second_account_cannot_take_the_same_number_in_another_spelling(client, db):
    """Registering "9876543210" then "+919876543210" is the same handset."""
    assert _register(client, phone="9876543210").status_code == 201

    res = _register(client, phone="+91 98765 43210")

    assert res.status_code == 409
    assert db.query(User).count() == 1


# ─── EXISTING ACCOUNTS MUST STILL LOG IN ─────────────

def _legacy_user(db, phone="9876543210"):
    """An account from before /auth/register normalised what it stored."""
    user = User(name="Legacy", phone=phone, role=UserRole.CUSTOMER,
                password_hash=hash_password("hunter2hunter2"))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def test_an_account_stored_before_normalization_is_not_locked_out(client, db):
    """
    /auth/register used to store whatever was typed. Those rows still exist;
    validating the login input must not lock them out - the number is the only
    way into the account.

    Getting past 401 is the whole assertion. /auth/login is two-step - it
    verifies the password and then sends an OTP - so a customer on an unknown
    device cannot reach 200 here however valid their credentials, and in this
    suite SMS is disabled so the OTP send itself fails. Neither says anything
    about whether the legacy row was found. 401 is the answer that would, and
    it is the one that must not appear.
    """
    _legacy_user(db)

    res = client.post("/auth/login",
                      json={"phone": "+919876543210", "password": "hunter2hunter2"})

    assert res.status_code != 401, "a pre-normalisation account was locked out"


def test_an_account_stored_before_normalization_logs_in_from_a_trusted_device(client, db):
    """
    The same account, all the way to a token.

    A trusted device skips the OTP step, which is the only path to a completed
    login that does not depend on an SMS this suite deliberately cannot send.
    That makes this the test that proves a legacy row logs in, rather than
    merely that it is not rejected.
    """
    user = _legacy_user(db)
    db.add(TrustedDevice(user_id=user.id, device_fingerprint="known-device",
                         device_name="Chrome on Windows"))
    db.commit()

    res = client.post("/auth/login", json={
        "phone": "+919876543210",
        "password": "hunter2hunter2",
        "device_fingerprint": "known-device",
    })

    assert res.status_code == 200, res.text
    body = res.json()
    assert body["requires_otp"] is False
    assert body["access_token"]
    assert body["user"]["id"] == user.id


def test_lookup_values_covers_the_spellings_that_exist_in_the_wild(client):
    assert lookup_values("9876543210") == [
        "+919876543210", "9876543210", "919876543210", "09876543210",
    ]


# ─── EMAIL ───────────────────────────────────────────

@pytest.mark.parametrize("email,stored", [
    ("priya@example.com", "priya@example.com"),
    ("  priya@example.com  ", "priya@example.com"),
    ("p.r+tag@sub.example.co.in", "p.r+tag@sub.example.co.in"),
])
def test_valid_emails_are_accepted_and_trimmed(client, db, email, stored):
    res = _register(client, email=email)

    assert res.status_code == 201, res.text
    assert db.query(User).first().email == stored


@pytest.mark.parametrize("missing", [None, "", "   "])
def test_email_remains_optional(client, db, missing):
    """Sign-up works without one, and blank means absent rather than malformed."""
    res = _register(client, email=missing)

    assert res.status_code == 201, res.text
    assert db.query(User).first().email is None


@pytest.mark.parametrize("bad", [
    "not-an-email", "priya@", "@example.com", "priya@example",
    "priya example@test.com", "priya@@example.com", "priya@.com",
])
def test_malformed_emails_are_rejected_with_422(client, db, bad):
    res = _register(client, email=bad)

    assert res.status_code == 422, f"accepted {bad!r}"
    assert db.query(User).count() == 0


# ─── UNRELATED BEHAVIOUR IS UNCHANGED ────────────────

def test_a_wrong_password_is_still_401_not_422(client, db):
    """Validation must not turn a failed login into a validation error."""
    make_user(db, "Priya", UserRole.CUSTOMER, phone=NORMALIZED)

    res = client.post("/auth/login", json={"phone": VALID, "password": "wrong-password"})

    assert res.status_code == 401


def test_an_unknown_but_valid_number_is_still_401(client, db):
    res = client.post("/auth/login", json={"phone": "9000000000", "password": "x"})

    assert res.status_code == 401
    assert "Invalid phone or password" in res.json()["detail"]


def test_registration_still_returns_a_usable_token(client, db):
    res = _register(client)

    assert res.status_code == 201
    token = res.json()["access_token"]
    me = client.get("/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["phone"] == NORMALIZED
