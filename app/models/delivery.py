from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, func
from app.db.base import Base


class DeliveryZone(Base):
    __tablename__ = "delivery_zones"

    id = Column(Integer, primary_key=True, index=True)
    area_name = Column(String, unique=True, nullable=False)
    charge = Column(Float, nullable=False, default=0.0)
    estimated_time = Column(Integer, nullable=False, default=60)  # minutes
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
