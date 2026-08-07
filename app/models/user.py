import enum
from sqlalchemy import Column, Integer, String, Boolean, DateTime, Enum, Date, func
from sqlalchemy.orm import relationship
from app.db.base import Base


class UserRole(str, enum.Enum):
    CUSTOMER = "customer"
    ADMIN = "admin"
    BAKER = "baker"
    RIDER = "rider"


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    phone = Column(String, unique=True, nullable=False, index=True)
    email = Column(String, nullable=True)
    date_of_birth = Column(Date, nullable=True)
    password_hash = Column(String, nullable=True)
    role = Column(Enum(UserRole), default=UserRole.CUSTOMER, nullable=False)
    is_active = Column(Boolean, default=True)
    on_duty = Column(Boolean, default=True)  # for bakers/riders — are they available for assignments?

    # ─── WhatsApp consent ────────────────────────────
    # WhatsApp Business policy requires an explicit opt-in before any
    # business-initiated message, and requires honouring opt-out. We must also
    # be able to *demonstrate* opt-in if Meta asks, so record when and how.
    # Defaults to False: never message someone we have no record for.
    whatsapp_opt_in = Column(Boolean, default=False, nullable=False)
    whatsapp_opt_in_at = Column(DateTime(timezone=True), nullable=True)
    whatsapp_opt_in_source = Column(String, nullable=True)   # "web_registration" | "whatsapp_reply" | "staff_onboarding"
    whatsapp_opt_out_at = Column(DateTime(timezone=True), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Relationships — must specify foreign_keys because orders has 3 FKs to users
    orders = relationship("Order", back_populates="user", foreign_keys="[Order.user_id]", lazy="selectin")

    @property
    def is_admin(self) -> bool:
        return self.role == UserRole.ADMIN

    @property
    def is_available(self) -> bool:
        """Baker/rider is active AND on duty."""
        return self.is_active and self.on_duty
