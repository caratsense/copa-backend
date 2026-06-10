from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.base import Base


class Product(Base):
    __tablename__ = "products"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    category = Column(String, nullable=False, index=True)
    description = Column(Text, nullable=True)
    base_price = Column(Float, nullable=False)
    is_customizable = Column(Boolean, default=True)
    is_available = Column(Boolean, default=True)
    image_url = Column(String, nullable=True)
    tags = Column(JSONB, default=list)
    section_id = Column(Integer, ForeignKey("menu_sections.id"), nullable=True, index=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    section = relationship("MenuSection", back_populates="products")
