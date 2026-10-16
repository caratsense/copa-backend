from sqlalchemy import Column, Integer, String, Float, Boolean, DateTime, Text, ForeignKey, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship
from app.db.base import Base

# Stamped on a product whose price is a stand-in for one the client has not
# supplied. Lives here because both the model and the routes need it, and a
# product's own state should not be defined in a deployment script.
PLACEHOLDER_PRICE_TAG = "draft-no-price"

# Stamped on a product that exists to carry a price, not to be bought: the
# "Vanilla Base Cake" and "Chocolate Base Cake" rows Build a Cake quotes from,
# and the premium rows beside them. They head a group of cakes rather than
# being a cake, which is why they read as a stray first item on the menu.
#
# They cannot simply be deleted or switched off. /build-a-cake/options looks
# the base rows up by name and refuses to serve the builder at all if either
# is missing or unavailable, so removing them would take the whole custom-cake
# flow down with them. Hiding them is the only safe answer, and a tag is how
# this codebase already says "true of the product, not of the catalogue".
PRICING_ROW_TAG = "pricing-row"


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
    # Whether `base_price` is a per-kg price that a SizeRule multiplies ("kg"),
    # or the price of the thing itself ("fixed") — a brownie, a loaf, a pack of
    # six buns. Defaults to "kg" because that is the assumption the pricing
    # engine was already making about every product; this column writes it down
    # rather than changing it. See app/services/pricing_engine.py.
    pricing_unit = Column(String, nullable=False, server_default="kg", default="kg")
    section_id = Column(Integer, ForeignKey("menu_sections.id"), nullable=True, index=True)
    sort_order = Column(Integer, default=0)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    @property
    def is_placeholder(self) -> bool:
        """
        Whether base_price is a stand-in rather than a price the client gave.

        Derived from the tag rather than stored, so there is exactly one place
        the answer lives and no second column to drift out of step with it. It
        is also why the question is not "is base_price == 1": a cake may one
        day genuinely cost a rupee.

        A placeholder is shown in the catalogue - the menu has to be reviewable
        before every price is in - but it is never available, so it cannot be
        ordered, and the product API refuses to publish it.
        """
        return PLACEHOLDER_PRICE_TAG in (self.tags or [])

    @property
    def is_pricing_row(self) -> bool:
        """
        Whether this row is a price for the builder rather than a cake to buy.

        Derived from the tag for the same reason as is_placeholder: one place
        holds the answer. These rows stay available and keep their price —
        Build a Cake depends on both — they are simply not offered as products
        anywhere a customer is choosing something to order.
        """
        return PRICING_ROW_TAG in (self.tags or [])

    section = relationship("MenuSection", back_populates="products")
    # The sizes/shapes this particular product is sold in. Empty for most
    # products, which fall back to the global SizeRule table (per-kg) or to a
    # single fixed price. See app/models/product_option.py.
    options = relationship(
        "ProductOption",
        back_populates="product",
        cascade="all, delete-orphan",
        order_by="(ProductOption.sort_order, ProductOption.id)",
        lazy="selectin",
    )
