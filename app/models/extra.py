from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, func
from app.db.base import Base


class Extra(Base):
    __tablename__ = "extras"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    price = Column(Float, default=0)
    image_url = Column(String, nullable=True)
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
