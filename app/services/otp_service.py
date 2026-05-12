"""
SMS OTP Service (Twilio)
=========================
- Generates 6-digit OTP
- Stores in Redis with TTL (5 min default)
- Sends via Twilio SMS
- Verifies OTP

SETUP:
1. Create Twilio account: https://www.twilio.com
2. Get Account SID, Auth Token, and a phone number
3. Add to .env:
   TWILIO_ENABLED=true
   TWILIO_ACCOUNT_SID=ACxxxxx
   TWILIO_AUTH_TOKEN=xxxxx
   TWILIO_PHONE_NUMBER=+1234567890

In dev/test mode (TWILIO_ENABLED=false), OTP is logged to console
and always accepts "000000" as valid — so you can test without Twilio.
"""

import random
import json
import redis
from app.config import get_settings

settings = get_settings()

OTP_PREFIX = "otp:"


def _get_redis():
    try:
        return redis.from_url(settings.REDIS_URL, decode_responses=True)
    except Exception:
        return None


def generate_otp() -> str:
    """Generate a 6-digit OTP."""
    return str(random.randint(100000, 999999))


def send_otp(phone: str, purpose: str = "login") -> dict:
    """
    Generate OTP, store in Redis, send via Twilio.
    Returns {"sent": True/False, "message": "...", "otp": "..." (only in dev mode)}
    """
    otp = generate_otp()
    r = _get_redis()

    # Store in Redis with expiry
    if r:
        key = f"{OTP_PREFIX}{phone}:{purpose}"
        r.setex(key, settings.OTP_EXPIRY_SECONDS, otp)

    # Send via Twilio
    if settings.TWILIO_ENABLED:
        try:
            from twilio.rest import Client
            client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
            message = client.messages.create(
                body=f"Your Copa Bakery verification code is: {otp}. Valid for {settings.OTP_EXPIRY_SECONDS // 60} minutes.",
                from_=settings.TWILIO_PHONE_NUMBER,
                to=phone,
            )
            print(f"[OTP] Sent to {phone} via Twilio: {message.sid}")
            return {"sent": True, "message": "OTP sent to your phone"}
        except Exception as e:
            print(f"[OTP] Twilio send failed: {e}")
            return {"sent": False, "message": f"Failed to send OTP: {str(e)}"}
    else:
        # Dev mode — log OTP to console
        print(f"[OTP] DEV MODE — OTP for {phone}: {otp}")
        return {"sent": True, "message": "OTP sent (dev mode)", "otp": otp}


def verify_otp(phone: str, otp: str, purpose: str = "login") -> bool:
    """
    Verify OTP against Redis.
    In dev mode with Twilio disabled, "000000" always passes.
    """
    # Dev bypass
    if not settings.TWILIO_ENABLED and otp == "000000":
        return True

    r = _get_redis()
    if not r:
        return False

    key = f"{OTP_PREFIX}{phone}:{purpose}"
    stored_otp = r.get(key)

    if stored_otp and stored_otp == otp:
        r.delete(key)  # one-time use
        return True

    return False


def invalidate_otp(phone: str, purpose: str = "login"):
    """Delete OTP (e.g. after too many failed attempts)."""
    r = _get_redis()
    if r:
        r.delete(f"{OTP_PREFIX}{phone}:{purpose}")
