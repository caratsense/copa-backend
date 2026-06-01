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

In dev/test mode (SMS_ENABLED=false), OTP is generated locally, logged to
the console, stored in Redis, and "000000" is always accepted.
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
    """Normalize to digits with the 91 country code for 2factor (e.g. 919876543210)."""
    clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    if len(clean) == 10:
        clean = "91" + clean
    return clean


def _enabled() -> bool:
    return bool(settings.SMS_ENABLED and settings.TWOFACTOR_API_KEY)


def send_otp(phone: str, purpose: str = "login") -> dict:
    """
    Send an OTP. With 2factor we use AUTOGEN — 2factor generates the OTP and
    returns a session id, which we store in Redis to verify against later.
    """
    if _enabled():
        try:
            normalized = _normalize_phone(phone)
            url = f"{TWOFACTOR_BASE}/{settings.TWOFACTOR_API_KEY}/SMS/{normalized}/AUTOGEN"
            if settings.TWOFACTOR_TEMPLATE:
                url = f"{url}/{settings.TWOFACTOR_TEMPLATE}"

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
    else:
        # Dev mode — generate OTP locally, store in Redis
        otp = generate_otp()
        r = _get_redis()
        if r:
            r.setex(f"{OTP_PREFIX}{phone}:{purpose}", OTP_EXPIRY, otp)
        logger.info(f"[OTP] DEV MODE — OTP for {phone}: {otp}")
        return {"sent": True, "message": "OTP sent (dev mode)", "otp": otp}


def verify_otp(phone: str, otp: str, purpose: str = "login") -> bool:
    """Verify an OTP against 2factor (prod) or Redis/"000000" (dev)."""
    # Dev bypass
    if not _enabled() and otp == "000000":
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
    else:
        # Dev mode — check Redis
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
