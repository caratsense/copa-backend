"""
SMS OTP Service (2factor.in)
============================
- Sends OTP via 2factor.in AUTOGEN API (2factor generates the OTP + a session id)
- We persist the returned session id in Redis (keyed by phone + purpose)
- Verification calls 2factor's VERIFY endpoint with that session id

SETUP:
1. Create account: https://2factor.in
2. Copy the API key from the dashboard
3. Add to .env:
   SMS_ENABLED=true
   TWOFACTOR_API_KEY=your-api-key
   # optional, only if you created a custom AUTOGEN template:
   TWOFACTOR_TEMPLATE=YourTemplateName

A locally-generated OTP (logged to the console, stored in Redis) and the fixed
DEV_TEST_OTP are available ONLY when the deployment explicitly opts in:

    ENVIRONMENT=development        # any non-production value
    DEV_ALLOW_TEST_OTP=true

Missing SMS credentials do NOT enable them. That used to be the rule, and it
meant every deploy without a 2factor key accepted "000000" for every account —
which, together with the passwordless /auth/login-otp endpoint, made a phone
number sufficient to obtain an admin session. When SMS is unavailable and the
deployment has not opted in, sending fails and login fails with it.
"""

import random
import re
import logging
from urllib.parse import quote

import httpx
import redis
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

OTP_PREFIX = "otp:"            # dev-mode OTP store
SESSION_PREFIX = "2f:"         # 2factor session-id store
FAIL_PREFIX = "otpfail:"       # wrong guesses against the current code
SEND_PREFIX = "otpsend:"       # codes sent to a handset in the current window
OTP_EXPIRY = settings.OTP_EXPIRY_SECONDS or 300

# A six-digit code is only as strong as the number of guesses allowed at it.
# Until these existed the only limit was per client IP, and the IP the app sees
# is Railway's proxy, so there was effectively none: /auth/login-otp and
# /auth/reset-password take nothing but a phone number and a code, for staff
# accounts too. These counters are per handset and do not care where the
# guesses come from.
#
# Five wrong guesses burn the code; five codes per handset per hour. That is at
# most 25 guesses an hour at a million possibilities, and it also stops the
# endpoints being used to flood one number with SMS.
MAX_VERIFY_ATTEMPTS = 5
MAX_SENDS_PER_WINDOW = 5
SEND_WINDOW_SECONDS = 3600

TWOFACTOR_BASE = "https://2factor.in/API/V1"


def _get_redis():
    try:
        return redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return None


def generate_otp() -> str:
    """Generate a 6-digit OTP for dev mode."""
    return str(random.randint(100000, 999999))


def _normalize_phone(phone: str) -> str:
    """2factor wants a bare 10-digit Indian number (no +91). Strip the country code."""
    clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    if len(clean) == 12 and clean.startswith("91"):
        clean = clean[2:]
    return clean


def _enabled() -> bool:
    """True when a real SMS provider is configured."""
    return bool(settings.SMS_ENABLED and settings.TWOFACTOR_API_KEY)


def _test_mode() -> bool:
    """
    True only when this deployment deliberately allows a stand-in OTP.

    Both switches are required (see Settings.test_otp_allowed). Never inferred
    from an absent SMS provider.
    """
    return settings.test_otp_allowed


def _handset(phone: str) -> str:
    """One key per handset, however the number was spelled on the way in."""
    return _normalize_phone(phone)


def _send_allowed(r, phone: str) -> bool:
    """Count this send against the handset's window; False once it is used up."""
    if not r:
        return True
    key = f"{SEND_PREFIX}{_handset(phone)}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, SEND_WINDOW_SECONDS)
    return count <= MAX_SENDS_PER_WINDOW


def _locked_out(r, phone: str, purpose: str) -> bool:
    if not r:
        return False
    return int(r.get(f"{FAIL_PREFIX}{_handset(phone)}:{purpose}") or 0) >= MAX_VERIFY_ATTEMPTS


def _record_failure(r, phone: str, purpose: str) -> None:
    """Count a wrong guess; on the last one allowed, burn the code outright."""
    if not r:
        return
    key = f"{FAIL_PREFIX}{_handset(phone)}:{purpose}"
    count = r.incr(key)
    if count == 1:
        r.expire(key, OTP_EXPIRY)
    if count >= MAX_VERIFY_ATTEMPTS:
        r.delete(f"{OTP_PREFIX}{phone}:{purpose}", f"{SESSION_PREFIX}{phone}:{purpose}")
        logger.warning("[OTP] Code for %s:%s burned after %d wrong attempts", phone, purpose, count)


def send_otp(phone: str, purpose: str = "login") -> dict:
    """
    Send an OTP. With 2factor we use AUTOGEN — 2factor generates the OTP and
    returns a session id, which we store in Redis to verify against later.
    """
    r = _get_redis()
    if not _send_allowed(r, phone):
        logger.warning("[OTP] Send refused for %s: %d codes already sent this hour",
                       phone, MAX_SENDS_PER_WINDOW)
        return {
            "sent": False,
            "throttled": True,
            "message": "Too many codes requested for this number. Please try again in an hour.",
        }
    # A fresh code gets a fresh set of attempts.
    if r:
        r.delete(f"{FAIL_PREFIX}{_handset(phone)}:{purpose}")

    if _enabled():
        try:
            normalized = _normalize_phone(phone)
            # 2factor: /SMS/{10-digit}/AUTOGEN/{template}. Template "OTP1" is the
            # built-in default — without it 2factor falls back to a voice call.
            template = settings.TWOFACTOR_TEMPLATE or "OTP1"
            url = f"{TWOFACTOR_BASE}/{settings.TWOFACTOR_API_KEY}/SMS/{normalized}/AUTOGEN/{template}"

            resp = httpx.get(url, timeout=15)
            data = resp.json()

            if data.get("Status") == "Success":
                session_id = data.get("Details")
                if r:
                    r.setex(f"{SESSION_PREFIX}{phone}:{purpose}", OTP_EXPIRY, session_id)
                logger.info(f"[OTP] Sent to {normalized} via 2factor (session {session_id})")
                return {"sent": True, "message": "OTP sent to your phone"}

            logger.error(f"[OTP] 2factor send failed: {data}")
            return {"sent": False, "message": data.get("Details", "Failed to send OTP")}

        except Exception as e:
            logger.error(f"[OTP] 2factor error: {e}")
            return {"sent": False, "message": "SMS service error. Please try again."}
    if not _test_mode():
        # No SMS provider and no explicit opt-in. Report the outage instead of
        # pretending an OTP was delivered; the caller turns this into a 503 so
        # login fails closed rather than waiting for a code that never arrives.
        logger.error(
            "[OTP] Cannot send: SMS_ENABLED=%s, TWOFACTOR_API_KEY %s, "
            "ENVIRONMENT=%s, DEV_ALLOW_TEST_OTP=%s",
            settings.SMS_ENABLED,
            "set" if settings.TWOFACTOR_API_KEY else "MISSING",
            settings.ENVIRONMENT, settings.DEV_ALLOW_TEST_OTP,
        )
        return {"sent": False, "message": "OTP service is unavailable. Please try again later."}

    # Explicit development/test mode — generate locally, store in Redis, log it.
    otp = generate_otp()
    if r:
        r.setex(f"{OTP_PREFIX}{phone}:{purpose}", OTP_EXPIRY, otp)
    logger.info(f"[OTP] TEST MODE — OTP for {phone}: {otp}")
    return {"sent": True, "message": "OTP sent (test mode)", "otp": otp}


_CODE = re.compile(r"[0-9]{4,8}")


def is_well_formed_code(otp) -> bool:
    """
    An OTP is a short run of ASCII digits and nothing else.

    The code is placed in the path of the 2factor verify URL, so anything that
    is not plainly a code must never get that far: the request is only as
    trustworthy as the URL it goes to.
    """
    return isinstance(otp, str) and _CODE.fullmatch(otp) is not None


def verify_otp(phone: str, otp: str, purpose: str = "login") -> bool:
    """Verify an OTP against 2factor, or against the test store when opted in."""
    if not is_well_formed_code(otp):
        logger.warning("[OTP] Rejected malformed code for %s:%s", phone, purpose)
        return False

    # The fixed test OTP. Gated on the deployment opting in — NOT on whether an
    # SMS provider happens to be configured.
    if _test_mode() and otp == settings.DEV_TEST_OTP:
        logger.warning("[OTP] Accepted DEV_TEST_OTP for %s — test mode is enabled", phone)
        return True

    r = _get_redis()
    if _locked_out(r, phone, purpose):
        logger.warning("[OTP] Too many wrong attempts for %s:%s — a new code is needed", phone, purpose)
        return False

    if _enabled():
        session_id = r.get(f"{SESSION_PREFIX}{phone}:{purpose}") if r else None
        if not session_id:
            logger.warning(f"[OTP] No active session for {phone}:{purpose}")
            return False
        try:
            url = (f"{TWOFACTOR_BASE}/{settings.TWOFACTOR_API_KEY}/SMS/VERIFY/"
                   f"{quote(str(session_id), safe='')}/{quote(otp, safe='')}")
            resp = httpx.get(url, timeout=15)
            data = resp.json()
            if data.get("Status") == "Success":
                if r:
                    r.delete(f"{SESSION_PREFIX}{phone}:{purpose}",
                             f"{FAIL_PREFIX}{_handset(phone)}:{purpose}")
                logger.info(f"[OTP] Verified for {phone}")
                return True
            logger.warning(f"[OTP] Verification failed for {phone}: {data}")
            _record_failure(r, phone, purpose)
            return False
        except Exception as e:
            logger.error(f"[OTP] 2factor verify error: {e}")
            return False
    if not _test_mode():
        # No provider, no opt-in: nothing can legitimately verify here.
        logger.error("[OTP] Verification attempted with no SMS provider and test mode off")
        return False

    # Explicit test mode — check the locally-generated code.
    if not r:
        return False
    key = f"{OTP_PREFIX}{phone}:{purpose}"
    stored_otp = r.get(key)
    if stored_otp and stored_otp == otp:
        r.delete(key, f"{FAIL_PREFIX}{_handset(phone)}:{purpose}")
        return True
    _record_failure(r, phone, purpose)
    return False


def resend_otp(phone: str, retry_type: str = "text") -> dict:
    """Resend OTP — simply request a fresh AUTOGEN session."""
    return send_otp(phone)


def invalidate_otp(phone: str, purpose: str = "login"):
    """Clear any stored OTP/session for this phone+purpose."""
    r = _get_redis()
    if r:
        r.delete(f"{OTP_PREFIX}{phone}:{purpose}")
        r.delete(f"{SESSION_PREFIX}{phone}:{purpose}")
