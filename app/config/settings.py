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

    # Gemini AI
    GEMINI_API_KEY: str = ""
    GEMINI_MODEL: str = "gemini-2.0-flash"

    # Twilio SMS OTP
    TWILIO_ENABLED: bool = False
    TWILIO_ACCOUNT_SID: str = ""
    TWILIO_AUTH_TOKEN: str = ""
    TWILIO_PHONE_NUMBER: str = ""    # your Twilio phone number e.g. "+1234567890"
    OTP_EXPIRY_SECONDS: int = 300    # 5 minutes

    @property
    def cors_origins_list(self) -> list[str]:
        return [o.strip() for o in self.CORS_ORIGINS.split(",") if o.strip()]

    class Config:
        env_file = ".env"


@lru_cache()
def get_settings() -> Settings:
    return Settings()
