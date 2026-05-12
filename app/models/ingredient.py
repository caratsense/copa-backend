from sqlalchemy import Column, Integer, String, Text, Boolean, DateTime, Float, func
from app.db.base import Base


class Ingredient(Base):
    __tablename__ = "ingredients"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    image_url = Column(String, nullable=True)
    description = Column(Text, nullable=True)          # short description
    story = Column(Text, nullable=True)                # longer story about sourcing/quality
    category = Column(String, nullable=True)           # "dairy", "chocolate", "fruit", "dry-fruit", "flour"
    is_premium = Column(Boolean, default=False)        # highlight as premium ingredient
    is_active = Column(Boolean, default=True)
    sort_order = Column(Integer, default=0)            # control display order in carousel
    created_at = Column(DateTime(timezone=True), server_default=func.now())
