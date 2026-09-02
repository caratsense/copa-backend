from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    APP_NAME: str = "Copa Bakery Backend"
    DEBUG: bool = True
    SECRET_KEY: str = "change-me-in-production"   # unused; kept so old .env files don't error

    # Deployment environment. Defaults to "production" deliberately: an
    # unconfigured deploy must get the SAFE behaviour, not the convenient one.
    # Recognised: "production" | "development" | "test" | "local" | "staging".
    ENVIRONMENT: str = "production"

    # Database
    DATABASE_URL: str = "postgresql://copa:copa_secret@db:5432/copa_db"
    REDIS_URL: str = "redis://redis:6379/0"

    # JWT Auth
    # This default is published in the repository, so it is not a secret: anyone
    # can forge an admin token with it. `_validate()` refuses to start in
    # production while it (or any other known placeholder) is still in use.
    JWT_SECRET: str = "super-secret-jwt-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 1440  # 24 hours

    # CORS — comma-separated list of allowed frontend origins.
    # MUST include your deployed frontend URL in production.
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173,http://localhost:8080"
    # Extra origins matched by regex (e.g. Vercel preview deploys).
    CORS_ORIGIN_REGEX: str = r"https://.*\.vercel\.app"

    # Admin bootstrap — /users/promote-admin is DISABLED unless this is set.
    PROMOTE_SECRET: str = ""

    # File uploads
    UPLOAD_DIR: str = "/code/uploads"
    MAX_UPLOAD_MB: int = 10

    # Bakery pickup point — the origin stamped on a delivery when it starts.
    # Defaults to the Lucknow store coordinates the tracking UI already centres
    # on; override per-deployment rather than editing code.
    BAKERY_LAT: float = 26.8467
    BAKERY_LNG: float = 80.9462

    # AI
    AI_PROVIDER: str = "stub"  # "stub" | "openai" | "custom"

    # WhatsApp Business API
    WHATSAPP_ENABLED: bool = False
    WHATSAPP_PHONE_ID: str = ""
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_TRACKING_BASE_URL: str = "http://localhost:3000/track"
    WHATSAPP_REVIEW_BASE_URL: str = "http://localhost:3000/menu"
    # No default: a verification token committed to the repository is not a
    # secret. Webhook verification refuses to succeed while this is unset.
    WHATSAPP_WEBHOOK_VERIFY_TOKEN: str = ""
    WHATSAPP_BUSINESS_PHONE: str = "919554444462"
    # Meta App Secret — used to verify the X-Hub-Signature-256 header on incoming
    # webhooks. Without it, anyone who knows the webhook URL can forge messages
    # (including staff/admin commands). Find it in Meta App Dashboard → Settings → Basic.
    WHATSAPP_APP_SECRET: str = ""
    # Must match the language your templates were APPROVED under in WhatsApp
    # Manager. A mismatch (e.g. "en" here vs "en_US" there) fails every send
    # with error 132001.
    WHATSAPP_TEMPLATE_LANG: str = "en"
    # Graph API version. Meta deprecates versions on a schedule; bumping this
    # should not require a code change.
    WHATSAPP_API_VERSION: str = "v21.0"
    # Optional JSON map overriding the Meta template name (and optionally the
    # language) for any key in app/services/wa_templates.py. Lets whoever owns
    # the Meta dashboard name templates freely without a code change:
    #   {"order_confirmation": "coc_order_confirm_v2"}
    #   {"order_delivered": {"name": "coc_delivered", "language": "en_US"}}
    WHATSAPP_TEMPLATE_NAMES: str = ""

    # AI Parser (Groq — free, fast)
    GROQ_API_KEY: str = ""
    GEMINI_API_KEY: str = ""  # kept for backward compat
    GEMINI_MODEL: str = "gemini-2.0-flash"

    # PayU Payment Gateway (India)
    PAYU_KEY: str = ""
    PAYU_SALT: str = ""
    PAYU_ENV: str = "test"                 # "test" or "prod"
    # Simulated payments for local development ONLY. This must never be an
    # automatic fallback: missing PayU credentials used to silently mark every
    # ONLINE order PAID, so an unconfigured production deploy gave cakes away.
    # With this False and PayU unconfigured, payment fails closed instead.
    PAYU_ALLOW_DEMO_PAYMENTS: bool = False
    BACKEND_BASE_URL: str = "http://localhost:8000"   # this API's public URL (for PayU callbacks)
    # The site customers land on after paying. Optional: when blank it is derived
    # from WHATSAPP_TRACKING_BASE_URL by stripping the trailing "/track", which
    # is how it has always worked. Set it explicitly if that URL is ever changed
    # to something that does not end in /track, so PayU redirects do not silently
    # point at the wrong host.
    FRONTEND_BASE_URL: str = ""

    # SMS OTP — 2factor.in
    SMS_ENABLED: bool = False
    TWOFACTOR_API_KEY: str = ""
    TWOFACTOR_TEMPLATE: str = ""     # optional: AUTOGEN template name from 2factor dashboard
    OTP_EXPIRY_SECONDS: int = 300    # 5 minutes
    # Development/test OTP. Deliberately TWO independent switches: a non-production
    # ENVIRONMENT *and* an explicit opt-in. Previously any deploy without SMS
    # credentials silently accepted "000000" for every account, which combined
    # with the passwordless /auth/login-otp endpoint meant a phone number alone
    # was enough to obtain an admin session.
    DEV_ALLOW_TEST_OTP: bool = False
    DEV_TEST_OTP: str = "000000"
    # MSG91 (legacy — no longer used, kept so old .env files don't error)
    MSG91_AUTH_KEY: str = ""
    MSG91_TEMPLATE_ID: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    @property
    def frontend_base_url(self) -> str:
        """Where to send a browser after checkout. Explicit setting wins."""
        if self.FRONTEND_BASE_URL.strip():
            return self.FRONTEND_BASE_URL.strip().rstrip("/")
        base = (self.WHATSAPP_TRACKING_BASE_URL or "").strip()
        if base.endswith("/track"):
            base = base[: -len("/track")]
        return base.rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT.strip().lower() not in _NON_PRODUCTION

    @property
    def test_otp_allowed(self) -> bool:
        """
        A fixed test OTP is honoured only when BOTH switches say so.

        Missing SMS credentials must never be a reason to accept one: that is an
        outage, not an invitation to skip authentication.
        """
        return (not self.is_production) and self.DEV_ALLOW_TEST_OTP

    def _validate(self) -> "Settings":
        """
        Refuse to start a production process with a secret anyone can read.

        Raised at import time (via get_settings) rather than on first login, so a
        misconfigured deploy fails its health check instead of quietly serving
        forgeable sessions.
        """
        if not self.is_production:
            return self

        secret = (self.JWT_SECRET or "").strip()
        if not secret:
            raise RuntimeError(
                "JWT_SECRET is not set. Refusing to start in "
                f"ENVIRONMENT={self.ENVIRONMENT!r}. Generate one with: "
                "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        if secret in _KNOWN_WEAK_SECRETS or any(m in secret.lower() for m in _WEAK_MARKERS):
            raise RuntimeError(
                "JWT_SECRET is a placeholder published in the repository, so any "
                "session signed with it can be forged. Refusing to start in "
                f"ENVIRONMENT={self.ENVIRONMENT!r}. Generate one with: "
                "python -c \"import secrets; print(secrets.token_urlsafe(48))\""
            )
        if len(secret) < _MIN_SECRET_LEN:
            raise RuntimeError(
                f"JWT_SECRET is only {len(secret)} characters; at least "
                f"{_MIN_SECRET_LEN} are required in ENVIRONMENT={self.ENVIRONMENT!r}."
            )

        if self.DEV_ALLOW_TEST_OTP:
            raise RuntimeError(
                "DEV_ALLOW_TEST_OTP is enabled in "
                f"ENVIRONMENT={self.ENVIRONMENT!r}. A fixed test OTP would let "
                "anyone log in as any account. Unset it, or set ENVIRONMENT to a "
                "non-production value."
            )
        return self

    class Config:
        env_file = ".env"


# Anything not in this set counts as production, so a typo fails safe.
_NON_PRODUCTION = {"development", "dev", "local", "test", "testing", "ci", "staging"}

_KNOWN_WEAK_SECRETS = {
    "super-secret-jwt-key-change-in-production",
    "change-me-in-production",
    # This project's own .env.example shipped this one, so it is the value a
    # deployment that copied that file is most likely to be running. It is 27
    # characters and contains none of the markers below, so it would otherwise
    # have passed every check here.
    "your-secret-key-change-this",
    "secret", "changeme", "change-me", "test", "dev",
}
_WEAK_MARKERS = ("change-in-production", "change-me-in-production", "changeme-in-production")
_MIN_SECRET_LEN = 16


@lru_cache()
def get_settings() -> Settings:
    return Settings()._validate()
