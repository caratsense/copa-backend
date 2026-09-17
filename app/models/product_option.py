from sqlalchemy import (
    Column, Integer, String, Float, Boolean, DateTime, ForeignKey,
    UniqueConstraint, func,
)
from sqlalchemy.orm import relationship
from app.db.base import Base


class ProductOption(Base):
    """
    A size, weight or shape one specific product is sold in.

    The global SizeRule table is shared by every per-kg product, so it can only
    say "these six weights, for everything". It cannot say that one cake starts
    at 700g, that another is not sold under 1kg, or that a tea cake comes as a
    500g loaf or a 1kg round at two unrelated prices.

    A product with options is priced ONLY from its options: the global SizeRule
    table is not consulted for it at all. That is what makes a size
    unselectable - it simply is not one of the product's options - without the
    pricing engine knowing anything about particular products.

    Exactly one of `price` and `multiplier` is set:

      multiplier - the option's price is product.base_price * multiplier. For a
                   per-kg cake whose price really is per kg and whose options
                   are just which weights it comes in. "700g" on a Rs 2,300/kg
                   cake is multiplier 0.7, and the kg price is not duplicated
                   here, so correcting it on the product corrects every option.

      price      - the option's price outright, and base_price is ignored. For
                   products whose sizes are not proportional: a 1.3kg chiffon
                   cake at Rs 2,400 is not Rs 2,400/kg, and a tea cake sold as
                   a Rs 800 loaf or a Rs 1,700 round is not one price scaled.
    """

    __tablename__ = "product_options"
    __table_args__ = (
        UniqueConstraint("product_id", "label", name="uq_product_option_label"),
    )

    id = Column(Integer, primary_key=True, index=True)
    product_id = Column(
        Integer, ForeignKey("products.id", ondelete="CASCADE"),
        nullable=False, index=True,
    )
    # What the customer picks: "700g", "1.5kg", "500g loaf", "1kg round".
    label = Column(String, nullable=False)
    price = Column(Float, nullable=True)
    multiplier = Column(Float, nullable=True)
    sort_order = Column(Integer, nullable=False, default=0)
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    product = relationship("Product", back_populates="options")

    def price_for(self, base_price: float) -> float:
        """What this option costs, before flavour/design/addons/rush."""
        if self.price is not None:
            return round(float(self.price), 2)
        return round(float(base_price) * float(self.multiplier or 1.0), 2)
