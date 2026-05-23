"""
SMS OTP Service (MSG91)
========================
- Sends OTP via MSG91 SendOTP API
- MSG91 handles OTP generation, storage, and verification
- No Redis needed for OTP storage — MSG91 manages it server-side

SETUP:
1. Create account: https://msg91.com/signup
2. Get AuthKey from: Dashboard → API → Configure
3. Create OTP template at: Dashboard → OTP → Templates
4. Add to .env:
   SMS_ENABLED=true
   MSG91_AUTH_KEY=your-auth-key
   MSG91_TEMPLATE_ID=your-otp-template-id

In dev/test mode (SMS_ENABLED=false), OTP is logged to console
and always accepts "000000" as valid.
"""

import random
import logging
import httpx
import redis
from app.config import get_settings

settings = get_settings()
logger = logging.getLogger(__name__)

OTP_PREFIX = "otp:"
OTP_EXPIRY = 300  # 5 minutes

MSG91_SEND_URL = "https://control.msg91.com/api/v5/otp"
MSG91_VERIFY_URL = "https://control.msg91.com/api/v5/otp/verify"
MSG91_RESEND_URL = "https://control.msg91.com/api/v5/otp/retry"


def _get_redis():
    try:
        return redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return None


def generate_otp() -> str:
    """Generate a 6-digit OTP for dev mode."""
    return str(random.randint(100000, 999999))


def _normalize_phone(phone: str) -> str:
    """Normalize phone to 91XXXXXXXXXX format for MSG91."""
    clean = phone.replace("+", "").replace(" ", "").replace("-", "")
    if len(clean) == 10:
        clean = "91" + clean
    return clean


def send_otp(phone: str, purpose: str = "login") -> dict:
    """
    Send OTP via MSG91.
    MSG91 generates and stores the OTP — we don't need to manage it.
    """
    normalized = _normalize_phone(phone)

    if settings.SMS_ENABLED and settings.MSG91_AUTH_KEY:
        try:
            payload = {
                "mobile": normalized,
                "template_id": settings.MSG91_TEMPLATE_ID,
                "otp_length": 6,
                "otp_expiry": 5,  # 5 minutes
            }

            resp = httpx.post(
                MSG91_SEND_URL,
                headers={
                    "authkey": settings.MSG91_AUTH_KEY,
                    "Content-Type": "application/json",
                },
                json=payload,
                timeout=15,
            )
            data = resp.json()

            if data.get("type") == "success":
                logger.info(f"[OTP] Sent to {normalized} via MSG91: {data.get('request_id')}")
                return {"sent": True, "message": "OTP sent to your phone"}
            else:
                logger.error(f"[OTP] MSG91 failed: {data}")
                return {"sent": False, "message": data.get("message", "Failed to send OTP")}

        except Exception as e:
            logger.error(f"[OTP] MSG91 error: {e}")
            return {"sent": False, "message": "SMS service error. Please try again."}
    else:
        # Dev mode — generate OTP locally, store in Redis
        otp = generate_otp()
        r = _get_redis()
        if r:
            key = f"{OTP_PREFIX}{phone}:{purpose}"
            r.setex(key, OTP_EXPIRY, otp)
        logger.info(f"[OTP] DEV MODE — OTP for {phone}: {otp}")
        return {"sent": True, "message": "OTP sent (dev mode)", "otp": otp}


def verify_otp(phone: str, otp: str, purpose: str = "login") -> bool:
    """
    Verify OTP.
    In production: verify against MSG91 API.
    In dev mode: check Redis or accept "000000".
    """
    # Dev bypass
    if not settings.SMS_ENABLED and otp == "000000":
        return True

    if settings.SMS_ENABLED and settings.MSG91_AUTH_KEY:
        try:
            normalized = _normalize_phone(phone)
            resp = httpx.get(
                MSG91_VERIFY_URL,
                params={"mobile": normalized, "otp": otp},
                headers={"authkey": settings.MSG91_AUTH_KEY},
                timeout=15,
            )
            data = resp.json()

            if data.get("type") == "success":
                logger.info(f"[OTP] Verified for {normalized}")
                return True
            else:
                logger.warning(f"[OTP] Verification failed for {normalized}: {data}")
                return False

        except Exception as e:
            logger.error(f"[OTP] MSG91 verify error: {e}")
            return False
    else:
        # Dev mode — check Redis
        if otp == "000000":
            return True
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
    """
    Resend OTP via MSG91.
    retry_type: "text" for SMS, "voice" for voice call
    """
    if settings.SMS_ENABLED and settings.MSG91_AUTH_KEY:
        try:
            normalized = _normalize_phone(phone)
            resp = httpx.post(
                MSG91_RESEND_URL,
                headers={
                    "authkey": settings.MSG91_AUTH_KEY,
                    "Content-Type": "application/json",
                },
                json={"mobile": normalized, "retrytype": retry_type},
                timeout=15,
            )
            data = resp.json()
            if data.get("type") == "success":
                return {"sent": True, "message": "OTP resent"}
            return {"sent": False, "message": data.get("message", "Resend failed")}
        except Exception as e:
            logger.error(f"[OTP] MSG91 resend error: {e}")
            return {"sent": False, "message": "Resend failed"}
    else:
        return send_otp(phone)


def invalidate_otp(phone: str, purpose: str = "login"):
    """Delete OTP from Redis (dev mode only)."""
    r = _get_redis()
    if r:
        r.delete(f"{OTP_PREFIX}{phone}:{purpose}")
