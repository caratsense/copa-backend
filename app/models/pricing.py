from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, func
from app.db.base import Base


class SizeRule(Base):
    """Size multiplies the base price. e.g. 1kg = 1.0x, 2kg = 1.8x"""
    __tablename__ = "size_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)       # "500g", "1kg", "2kg"
    multiplier = Column(Float, nullable=False, default=1.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class FlavorRule(Base):
    """Extra cost added for premium flavors."""
    __tablename__ = "flavor_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)       # "vanilla", "chocolate", "red-velvet"
    extra_cost = Column(Float, nullable=False, default=0.0)
    # Which sponge this flavour is made on: "vanilla", "chocolate", or NULL for
    # a flavour that is not part of the sponge-first Build-a-Cake flow.
    #
    # Stored rather than inferred from the name, because the name does not say.
    # "White Chocolate Raspberry Pistachio" is a VANILLA-sponge flavour and
    # "Vanilla Orange Chocolate Crumble" is too - a rule that looked for
    # "chocolate" in the name would get both wrong, and would do it silently,
    # offering a customer a flavour the kitchen cannot make on that sponge.
    sponge = Column(String, nullable=True, index=True)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class DesignRule(Base):
    """Cost for design complexity."""
    __tablename__ = "design_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)       # "basic", "semi-custom", "full-custom"
    cost = Column(Float, nullable=False, default=0.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class AddonRule(Base):
    """Optional add-ons like toppers, candles, message plates."""
    __tablename__ = "addon_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)       # "topper", "candles", "photo-print"
    cost = Column(Float, nullable=False, default=0.0)
    stock = Column(Integer, nullable=True)                   # None = unlimited; 0 = out of stock
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())


class RushRule(Base):
    """Extra charge for rush / same-day / express orders."""
    __tablename__ = "rush_rules"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, unique=True, nullable=False)       # "standard", "same-day", "express-2hr"
    cost = Column(Float, nullable=False, default=0.0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
