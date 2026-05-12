from sqlalchemy import Column, Integer, String, DateTime, ForeignKey, func
from app.db.base import Base


class TrustedDevice(Base):
    """
    Tracks devices that have completed OTP verification.
    Customers skip OTP on trusted devices.
    Staff always require OTP regardless.
    """
    __tablename__ = "trusted_devices"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    device_fingerprint = Column(String, nullable=False, index=True)
    # fingerprint = hash of user-agent + IP or a frontend-generated device ID
    device_name = Column(String, nullable=True)    # "Chrome on Windows", "iPhone Safari"
    last_used_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
    created_at = Column(DateTime(timezone=True), server_default=func.now())
