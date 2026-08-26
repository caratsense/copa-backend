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
import logging
import httpx
import redis
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

OTP_PREFIX = "otp:"            # dev-mode OTP store
SESSION_PREFIX = "2f:"         # 2factor session-id store
OTP_EXPIRY = settings.OTP_EXPIRY_SECONDS or 300

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


def send_otp(phone: str, purpose: str = "login") -> dict:
    """
    Send an OTP. With 2factor we use AUTOGEN — 2factor generates the OTP and
    returns a session id, which we store in Redis to verify against later.
    """
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
                r = _get_redis()
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
    r = _get_redis()
    if r:
        r.setex(f"{OTP_PREFIX}{phone}:{purpose}", OTP_EXPIRY, otp)
    logger.info(f"[OTP] TEST MODE — OTP for {phone}: {otp}")
    return {"sent": True, "message": "OTP sent (test mode)", "otp": otp}


def verify_otp(phone: str, otp: str, purpose: str = "login") -> bool:
    """Verify an OTP against 2factor, or against the test store when opted in."""
    # The fixed test OTP. Gated on the deployment opting in — NOT on whether an
    # SMS provider happens to be configured.
    if _test_mode() and otp == settings.DEV_TEST_OTP:
        logger.warning("[OTP] Accepted DEV_TEST_OTP for %s — test mode is enabled", phone)
        return True

    if _enabled():
        r = _get_redis()
        session_id = r.get(f"{SESSION_PREFIX}{phone}:{purpose}") if r else None
        if not session_id:
            logger.warning(f"[OTP] No active session for {phone}:{purpose}")
            return False
        try:
            url = f"{TWOFACTOR_BASE}/{settings.TWOFACTOR_API_KEY}/SMS/VERIFY/{session_id}/{otp}"
            resp = httpx.get(url, timeout=15)
            data = resp.json()
            if data.get("Status") == "Success":
                if r:
                    r.delete(f"{SESSION_PREFIX}{phone}:{purpose}")
                logger.info(f"[OTP] Verified for {phone}")
                return True
            logger.warning(f"[OTP] Verification failed for {phone}: {data}")
            return False
        except Exception as e:
            logger.error(f"[OTP] 2factor verify error: {e}")
            return False
    if not _test_mode():
        # No provider, no opt-in: nothing can legitimately verify here.
        logger.error("[OTP] Verification attempted with no SMS provider and test mode off")
        return False

    # Explicit test mode — check the locally-generated code.
    r = _get_redis()
    if not r:
        return False
    key = f"{OTP_PREFIX}{phone}:{purpose}"
    stored_otp = r.get(key)
    if stored_otp and stored_otp == otp:
        r.delete(key)
        return True
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
