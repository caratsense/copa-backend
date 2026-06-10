import enum
from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Enum, func
from app.db.base import Base


class DiscountType(str, enum.Enum):
    FLAT = "flat"           # ₹100 off
    PERCENTAGE = "percentage"  # 10% off


class Coupon(Base):
    __tablename__ = "coupons"

    id = Column(Integer, primary_key=True, index=True)
    code = Column(String, unique=True, nullable=False, index=True)    # "WELCOME50"
    discount_type = Column(Enum(DiscountType), nullable=False)
    discount_value = Column(Float, nullable=False)                     # 50 or 10 (for %)
    min_order_value = Column(Float, default=0.0)                       # minimum cart value
    max_discount = Column(Float, nullable=True)                        # cap for % discounts
    max_uses = Column(Integer, nullable=True)                          # total uses allowed
    used_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    expires_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    def calculate_discount(self, order_total: float) -> float:
        """Returns the discount amount for a given order total."""
        if order_total < self.min_order_value:
            return 0.0
        if self.discount_type == DiscountType.FLAT:
            discount = min(self.discount_value, order_total)
        else:
            discount = order_total * (self.discount_value / 100)
            if self.max_discount:
                discount = min(discount, self.max_discount)
        return round(discount, 2)
