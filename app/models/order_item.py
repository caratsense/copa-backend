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

    @property
    def product_name(self) -> str | None:
        """
        The cake's name, for staff and customer screens.

        Read through the relationship rather than denormalised onto the row, so
        a renamed product reads correctly everywhere. Returns None when the
        relationship is not loaded (queries that use noload) instead of
        triggering a lazy fetch the caller did not ask for.
        """
        product = self.__dict__.get("product")
        return getattr(product, "name", None) if product is not None else None

    @property
    def product_image(self) -> str | None:
        """
        The catalogue photograph of the cake that was ordered.

        The staff screens described an order in words alone — "1kg Vanilla
        Cookie Cream Cake" — and the catalogue carries five vanilla cakes whose
        names differ by one word. Whoever is making it had nothing to check
        against, which is how the wrong cake gets made.

        Same reasoning as product_name: read through the relationship so a
        re-uploaded photograph is picked up everywhere at once, and None rather
        than a lazy fetch when the relationship was deliberately not loaded.
        """
        product = self.__dict__.get("product")
        return getattr(product, "image_url", None) if product is not None else None
