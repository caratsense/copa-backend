from sqlalchemy import Column, Integer, Float, ForeignKey, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.base import Base


class OrderItem(Base):
    __tablename__ = "order_items"

    id = Column(Integer, primary_key=True, index=True)
    order_id = Column(Integer, ForeignKey("orders.id", ondelete="CASCADE"), nullable=False, index=True)
    product_id = Column(Integer, ForeignKey("products.id"), nullable=False)
    quantity = Column(Integer, nullable=False, default=1)
    customization = Column(JSONB, default=dict)
    # Stores: {"size": "1kg", "flavor": "chocolate", "design": "full-custom",
    #          "addons": ["topper"], "rush": "standard"}
    price = Column(Float, nullable=False, default=0.0)  # calculated price for this line
    price_breakdown = Column(JSONB, default=dict)        # full breakdown stored for transparency
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships
    order = relationship("Order", back_populates="items")
    product = relationship("Product", lazy="selectin")
