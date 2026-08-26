"""
Authentication hardening: the demo-OTP bypass and the committed JWT secret.

Both used to be reachable in a default production deploy:

  * `SMS_ENABLED=false` (the shipped default) made the fixed OTP "000000" valid
    for every account. Combined with /auth/login-otp — which asks for a phone
    number and no password at all — a phone number was sufficient to obtain an
    admin session, and /auth/reset-password would then rewrite the password.

  * `JWT_SECRET` shipped with a value committed to the repository, so a valid
    admin token could be minted offline by anyone who had read the source.

The rule these tests pin down: a stand-in OTP requires the deployment to opt in
DELIBERATELY (non-production ENVIRONMENT *and* DEV_ALLOW_TEST_OTP). An absent
SMS provider is an outage, never a reason to skip authentication.
"""

import pytest

from app.config.settings import Settings
from app.models.user import UserRole
from app.services import otp_service

from tests.conftest import make_user


REAL_SECRET = "a-genuinely-random-secret-value-for-tests"
COMMITTED_DEFAULT = "super-secret-jwt-key-change-in-production"


def _settings(**kw) -> Settings:
    """A Settings instance with the fields under test set explicitly."""
    base = dict(JWT_SECRET=REAL_SECRET, ENVIRONMENT="production",
                DEV_ALLOW_TEST_OTP=False)
    base.update(kw)
    return Settings(**base)


@pytest.fixture
def otp_redis(monkeypatch, fake_redis):
    """Point the OTP store at the in-process fake Redis."""
    monkeypatch.setattr(otp_service, "_get_redis", lambda: fake_redis)
    return fake_redis


@pytest.fixture
def test_mode(monkeypatch):
    """Simulate a deployment that has deliberately opted into the test OTP."""
    monkeypatch.setattr(otp_service, "_test_mode", lambda: True)


# ─── CONFIGURATION VALIDATION ────────────────────────

def test_production_refuses_to_start_on_the_committed_jwt_secret():
    with pytest.raises(RuntimeError) as e:
        _settings(JWT_SECRET=COMMITTED_DEFAULT)._validate()
    assert "JWT_SECRET" in str(e.value)


def test_production_refuses_to_start_without_a_jwt_secret():
    with pytest.raises(RuntimeError):
        _settings(JWT_SECRET="")._validate()


def test_production_refuses_a_short_jwt_secret():
    with pytest.raises(RuntimeError) as e:
        _settings(JWT_SECRET="short")._validate()
    assert "characters" in str(e.value)


def test_production_refuses_to_start_with_the_test_otp_enabled():
    """Otherwise one stray env var silently reopens the whole bypass."""
    with pytest.raises(RuntimeError) as e:
        _settings(DEV_ALLOW_TEST_OTP=True)._validate()
    assert "DEV_ALLOW_TEST_OTP" in str(e.value)


def test_production_starts_with_a_real_secret():
    assert _settings()._validate().JWT_SECRET == REAL_SECRET


def test_development_may_use_whatever_it_likes():
    """Local work must not need a generated secret to run."""
    s = _settings(ENVIRONMENT="development", JWT_SECRET=COMMITTED_DEFAULT,
                  DEV_ALLOW_TEST_OTP=True)._validate()
    assert s.test_otp_allowed is True


@pytest.mark.parametrize("env", ["production", "PRODUCTION", "prod-eu", "typo", ""])
def test_unrecognised_environment_counts_as_production(env):
    """A typo in ENVIRONMENT must fail safe, not unlock the test OTP."""
    s = _settings(ENVIRONMENT=env, DEV_ALLOW_TEST_OTP=True)
    assert s.is_production is True
    assert s.test_otp_allowed is False


@pytest.mark.parametrize("env", ["development", "dev", "local", "test", "staging", "ci"])
def test_recognised_non_production_environments(env):
    assert _settings(ENVIRONMENT=env).is_production is False


def test_test_otp_needs_both_switches():
    """Neither switch alone is enough."""
    assert _settings(ENVIRONMENT="development", DEV_ALLOW_TEST_OTP=False).test_otp_allowed is False
    assert _settings(ENVIRONMENT="production", DEV_ALLOW_TEST_OTP=True).test_otp_allowed is False
    assert _settings(ENVIRONMENT="development", DEV_ALLOW_TEST_OTP=True).test_otp_allowed is True


# ─── MISSING SMS IS NOT A BYPASS ─────────────────────

def test_missing_sms_configuration_does_not_accept_the_test_otp(otp_redis):
    """
    The core regression. SMS_ENABLED=false and no TWOFACTOR_API_KEY is exactly
    how the application ships; it used to mean "000000" unlocked every account.
    """
    assert otp_service._enabled() is False, "precondition: no SMS provider"
    assert otp_service._test_mode() is False, "precondition: no opt-in"
    assert otp_service.verify_otp("+919554444462", "000000") is False


def test_missing_sms_configuration_reports_failure_instead_of_pretending(otp_redis):
    """`sent: True` with nothing delivered is what let the bypass hide."""
    result = otp_service.send_otp("+919554444462")
    assert result["sent"] is False
    assert "otp" not in result, "an OTP must never be handed back over the API here"


def test_login_fails_closed_when_no_otp_can_be_sent(client, db):
    """Better a 503 than a temp_token nobody can ever satisfy."""
    make_user(db, "Priya", UserRole.CUSTOMER, phone="+919222222222")
    r = client.post("/auth/login", json={"phone": "+919222222222", "password": "x"})
    assert r.status_code in (401, 503)


def test_passwordless_login_otp_cannot_reach_an_admin_session(client, db):
    """
    /auth/login-otp takes a phone number and no password. With the old bypass
    this plus "000000" was a complete account takeover of the seeded admin.
    """
    make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462")

    start = client.post("/auth/login-otp", json={"phone": "+919554444462"})
    assert start.status_code == 503, "an OTP must not be issued with no way to send it"

    # Even holding a temp_token, the fixed code must not verify.
    assert otp_service.verify_otp("+919554444462", "000000") is False


def test_password_reset_cannot_be_completed_with_the_test_otp(client, db, otp_redis):
    """The takeover's second half: rewrite the password once past the OTP."""
    make_user(db, "Shriya", UserRole.ADMIN, phone="+919554444462")
    r = client.post("/auth/reset-password", json={
        "phone": "+919554444462", "otp": "000000", "new_password": "attacker-owns-this"})
    assert r.status_code == 401


# ─── EXPLICIT TEST MODE STILL WORKS ──────────────────

def test_test_otp_works_when_deliberately_enabled(test_mode, otp_redis):
    assert otp_service.verify_otp("+919222222222", "000000") is True


def test_test_mode_still_rejects_a_wrong_code(test_mode, otp_redis):
    """Opting in must not accept *anything* — only the configured value."""
    assert otp_service.verify_otp("+919222222222", "111111") is False


# ─── REAL OTP BEHAVIOUR IS UNCHANGED ─────────────────

def test_generated_otp_round_trips(test_mode, otp_redis):
    sent = otp_service.send_otp("+919222222222")
    assert sent["sent"] is True
    code = sent["otp"]
    assert otp_service.verify_otp("+919222222222", code) is True


def test_generated_otp_is_single_use(test_mode, otp_redis):
    code = otp_service.send_otp("+919222222222")["otp"]
    assert otp_service.verify_otp("+919222222222", code) is True
    assert otp_service.verify_otp("+919222222222", code) is False


def test_generated_otp_is_scoped_to_its_phone_number(test_mode, otp_redis):
    code = otp_service.send_otp("+919222222222")["otp"]
    assert otp_service.verify_otp("+919333333333", code) is False


def test_generated_otp_is_scoped_to_its_purpose(test_mode, otp_redis):
    """A login code must not complete a password reset."""
    code = otp_service.send_otp("+919222222222", purpose="login")["otp"]
    assert otp_service.verify_otp("+919222222222", code, purpose="reset") is False


def test_generated_otp_is_six_digits(test_mode, otp_redis):
    code = otp_service.send_otp("+919222222222")["otp"]
    assert len(code) == 6 and code.isdigit()
