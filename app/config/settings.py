from pydantic_settings import BaseSettings
from functools import lru_cache


class Settings(BaseSettings):
    APP_NAME: str = "Copa Bakery Backend"
    DEBUG: bool = True
    SECRET_KEY: str = "change-me-in-production"

    # Database
    DATABASE_URL: str = "postgresql://copa:copa_secret@db:5432/copa_db"
    REDIS_URL: str = "redis://redis:6379/0"

    # JWT Auth
    JWT_SECRET: str = "super-secret-jwt-key-change-in-production"
    JWT_ALGORITHM: str = "HS256"
    JWT_EXPIRY_MINUTES: int = 1440  # 24 hours

    # CORS — add your frontend URL here
    CORS_ORIGINS: str = "http://localhost:3000,http://localhost:5173,http://localhost:8080"

    # File uploads
    UPLOAD_DIR: str = "/code/uploads"
    MAX_UPLOAD_MB: int = 10

    # AI
    AI_PROVIDER: str = "stub"  # "stub" | "openai" | "custom"

    # WhatsApp Business API
    WHATSAPP_ENABLED: bool = False
    WHATSAPP_PHONE_ID: str = ""
    WHATSAPP_TOKEN: str = ""
    WHATSAPP_TRACKING_BASE_URL: str = "http://localhost:3000/track"
    WHATSAPP_REVIEW_BASE_URL: str = "http://localhost:3000/menu"
    WHATSAPP_WEBHOOK_VERIFY_TOKEN: str = "cakeoclock2026"
    WHATSAPP_BUSINESS_PHONE: str = "919554444462"

    # AI Parser (Groq — free, fast)
    GROQ_API_KEY: str = ""
    GEMINI_API_KEY: str = ""  # kept for backward compat
    GEMINI_MODEL: str = "gemini-2.0-flash"

    # PayU Payment Gateway (India)
    PAYU_KEY: str = ""
    PAYU_SALT: str = ""
    PAYU_ENV: str = "test"                 # "test" or "prod"
    BACKEND_BASE_URL: str = "http://localhost:8000"   # this API's public URL (for PayU callbacks)

    # SMS OTP — 2factor.in
    SMS_ENABLED: bool = False
    TWOFACTOR_API_KEY: str = ""
    TWOFACTOR_TEMPLATE: str = ""     # optional: AUTOGEN template name from 2factor dashboard
    OTP_EXPIRY_SECONDS: int = 300    # 5 minutes
    # MSG91 (legacy — no longer used, kept so old .env files don't error)
    MSG91_AUTH_KEY: str = ""
    MSG91_TEMPLATE_ID: str = ""

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
