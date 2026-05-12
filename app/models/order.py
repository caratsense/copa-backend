import enum
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, func
from sqlalchemy.orm import relationship
from app.db.base import Base


class OrderStatus(str, enum.Enum):
    RECEIVED = "RECEIVED"
    CONFIRMED = "CONFIRMED"
    ASSIGNED = "ASSIGNED"
    IN_PRODUCTION = "IN_PRODUCTION"
    QC = "QC"
    AWAITING_APPROVAL = "AWAITING_APPROVAL"
    PACKAGED = "PACKAGED"
    OUT_FOR_DELIVERY = "OUT_FOR_DELIVERY"
    DELIVERED = "DELIVERED"
    CANCELLED = "CANCELLED"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    REFUNDED = "REFUNDED"


VALID_TRANSITIONS: dict[OrderStatus, list[OrderStatus]] = {
    OrderStatus.RECEIVED: [OrderStatus.CONFIRMED, OrderStatus.CANCELLED],
    OrderStatus.CONFIRMED: [OrderStatus.ASSIGNED, OrderStatus.CANCELLED],
    OrderStatus.ASSIGNED: [OrderStatus.IN_PRODUCTION, OrderStatus.CANCELLED],
    OrderStatus.IN_PRODUCTION: [OrderStatus.AWAITING_APPROVAL, OrderStatus.CANCELLED],
    OrderStatus.AWAITING_APPROVAL: [OrderStatus.PACKAGED, OrderStatus.IN_PRODUCTION, OrderStatus.CANCELLED],
    OrderStatus.PACKAGED: [OrderStatus.OUT_FOR_DELIVERY],
    OrderStatus.OUT_FOR_DELIVERY: [OrderStatus.DELIVERED],
    OrderStatus.DELIVERED: [],
    OrderStatus.CANCELLED: [],
}


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(Enum(OrderStatus), default=OrderStatus.RECEIVED, nullable=False)
    subtotal = Column(Float, nullable=False, default=0.0)
    discount = Column(Float, nullable=False, default=0.0)
    total_price = Column(Float, nullable=False, default=0.0)
    coupon_code = Column(String, nullable=True)
    delivery_address = Column(String, nullable=True)
    delivery_time = Column(DateTime(timezone=True), nullable=True)
    delivery_zone_id = Column(Integer, ForeignKey("delivery_zones.id"), nullable=True)
    payment_status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    assigned_baker_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    assigned_rider_id = Column(Integer, ForeignKey("users.id"), nullable=True, index=True)
    notes = Column(String, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())

    # Relationships
    user = relationship("User", back_populates="orders", foreign_keys=[user_id], lazy="selectin")
    baker = relationship("User", foreign_keys=[assigned_baker_id], lazy="selectin")
    rider = relationship("User", foreign_keys=[assigned_rider_id], lazy="selectin")
    items = relationship("OrderItem", back_populates="order", lazy="selectin", cascade="all, delete-orphan")
    events = relationship("OrderEvent", back_populates="order", lazy="selectin", cascade="all, delete-orphan")
