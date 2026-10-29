import enum
from sqlalchemy import Column, Integer, String, Float, DateTime, ForeignKey, Enum, func
from sqlalchemy.dialects.postgresql import JSONB
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
    # A delivery that was attempted and could not be completed - nobody home,
    # wrong address, customer unreachable. Without it the only way to close an
    # OUT_FOR_DELIVERY order was DELIVERED, so a failed attempt had to be
    # recorded as a success, which told the customer their cake had arrived.
    # Not terminal: the order goes back out, or is cancelled.
    DELIVERY_FAILED = "DELIVERY_FAILED"
    # ── Self pickup ──
    # A collected order is not a delivered one, and saying DELIVERED for a cake
    # the customer walked in for is the kind of small lie that makes the whole
    # trail untrustworthy. Before these existed, self pickup had no states of
    # its own: it was pushed through OUT_FOR_DELIVERY and DELIVERED, with a
    # rider assigned to a cake nobody was taking anywhere.
    READY_FOR_PICKUP = "READY_FOR_PICKUP"
    COLLECTED = "COLLECTED"
    # Nobody came. Recoverable, like DELIVERY_FAILED: a cake is still on the
    # counter and somebody has to decide what happens to it.
    NOT_COLLECTED = "NOT_COLLECTED"
    CANCELLED = "CANCELLED"


# How the order reaches the customer. A plain string rather than a second
# native enum, which is what `status` is and what makes adding a value to it a
# migration instead of a line of Python.
class Fulfilment(str, enum.Enum):
    DELIVERY = "DELIVERY"
    PICKUP = "PICKUP"


class PaymentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PAID = "PAID"
    COD_PENDING = "COD_PENDING"
    FAILED = "FAILED"
    REFUNDED = "REFUNDED"


VALID_TRANSITIONS: dict[OrderStatus, list[OrderStatus]] = {
    OrderStatus.RECEIVED: [OrderStatus.CONFIRMED, OrderStatus.CANCELLED],
    OrderStatus.CONFIRMED: [OrderStatus.ASSIGNED, OrderStatus.CANCELLED],
    OrderStatus.ASSIGNED: [OrderStatus.IN_PRODUCTION, OrderStatus.CANCELLED],
    OrderStatus.IN_PRODUCTION: [OrderStatus.AWAITING_APPROVAL, OrderStatus.CANCELLED],
    OrderStatus.AWAITING_APPROVAL: [OrderStatus.PACKAGED, OrderStatus.IN_PRODUCTION, OrderStatus.CANCELLED],
    # Packaged forks on how the order is being fulfilled. Which branch is
    # allowed is enforced against order.fulfilment, not here: this table says
    # what a status may become, not which one an order should take.
    OrderStatus.PACKAGED: [OrderStatus.OUT_FOR_DELIVERY, OrderStatus.READY_FOR_PICKUP],
    OrderStatus.OUT_FOR_DELIVERY: [OrderStatus.DELIVERED, OrderStatus.DELIVERY_FAILED],
    OrderStatus.DELIVERED: [],
    # A failed attempt is recoverable: send it out again, or give up on it.
    # Deliberately NOT straight to DELIVERED - a second attempt is a real
    # dispatch and should be recorded as one.
    OrderStatus.DELIVERY_FAILED: [OrderStatus.OUT_FOR_DELIVERY, OrderStatus.CANCELLED],
    # Waiting on the counter. It can be collected, or the slot can pass with
    # nobody coming — and a cake nobody collected must be able to say so
    # rather than sitting in READY_FOR_PICKUP for ever.
    OrderStatus.READY_FOR_PICKUP: [OrderStatus.COLLECTED, OrderStatus.NOT_COLLECTED,
                                   OrderStatus.CANCELLED],
    # Late is not the same as never: someone who turns up an hour after their
    # slot still gets their cake, so this goes back as well as forward.
    OrderStatus.NOT_COLLECTED: [OrderStatus.READY_FOR_PICKUP, OrderStatus.COLLECTED,
                                OrderStatus.CANCELLED],
    OrderStatus.COLLECTED: [],
    OrderStatus.CANCELLED: [],
}


class Order(Base):
    __tablename__ = "orders"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=False, index=True)
    status = Column(Enum(OrderStatus), default=OrderStatus.RECEIVED, nullable=False)
    # subtotal = items + extras + delivery. discount applies to items only.
    # total_price = subtotal - discount.
    subtotal = Column(Float, nullable=False, default=0.0)
    delivery_charge = Column(Float, nullable=False, default=0.0)
    extras_total = Column(Float, nullable=False, default=0.0)
    # Snapshot of chosen extras at order time: [{"id", "name", "price"}]
    extras = Column(JSONB, default=list)
    discount = Column(Float, nullable=False, default=0.0)
    total_price = Column(Float, nullable=False, default=0.0)
    coupon_code = Column(String, nullable=True)
    # DELIVERY or PICKUP. Before this column, self pickup was recorded by
    # throwing the fact away: both the website and the bot set delivery_address
    # to NULL for a pickup, which is indistinguishable from a delivery whose
    # address failed to save. Nothing downstream could tell, so pickups were
    # given a rider, shown on the live delivery map, and told their cake was
    # out for delivery.
    fulfilment = Column(String, nullable=False, default=Fulfilment.DELIVERY.value,
                        server_default=Fulfilment.DELIVERY.value, index=True)
    delivery_address = Column(String, nullable=True)
    delivery_time = Column(DateTime(timezone=True), nullable=True)
    delivery_zone_id = Column(Integer, ForeignKey("delivery_zones.id"), nullable=True)
    payment_status = Column(Enum(PaymentStatus), default=PaymentStatus.PENDING)
    # ONLINE for everything new. "COD" survives only on rows created before
    # cash on delivery was withdrawn; nothing can write it any more.
    payment_method = Column(String, default="ONLINE")
    payment_id = Column(String, nullable=True)  # Cashfree transaction ID
    # ── Proof of work ──
    # The baker photographs the finished cake before it can be sent for
    # approval, Shriya looks at that photo and verifies it, and only a verified
    # cake can be packed. Before this, "baked" was a button: the kitchen could
    # mark a cake done and it would move to packing with nobody having seen it.
    proof_image = Column(String, nullable=True)
    proof_verified_at = Column(DateTime(timezone=True), nullable=True)
    proof_verified_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    # The handover shot: the rider at the door, or the counter at pickup. An
    # order cannot be closed as delivered or collected without one, which is
    # the only evidence the shop has when a customer says it never arrived.
    handover_image = Column(String, nullable=True)
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

    @property
    def is_pickup(self) -> bool:
        """Read this rather than testing delivery_address for emptiness."""
        return (self.fulfilment or Fulfilment.DELIVERY.value) == Fulfilment.PICKUP.value
