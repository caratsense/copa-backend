from sqlalchemy import Column, Integer, String, Text, Boolean, Float, DateTime, ForeignKey, func
from sqlalchemy.orm import relationship
from app.db.base import Base


class Review(Base):
    __tablename__ = "reviews"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id"), nullable=True, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    rating = Column(Float, nullable=False)               # 1.0 to 5.0
    comment = Column(Text, nullable=True)
    image_url = Column(String, nullable=True)             # customer photo of the cake
    is_approved = Column(Boolean, default=False)          # admin approves before showing
    is_featured = Column(Boolean, default=False)          # admin picks for spotlight carousel
    admin_reply = Column(Text, nullable=True)             # bakery can reply to review
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    user = relationship("User", lazy="selectin")
    order = relationship("Order", lazy="selectin")
