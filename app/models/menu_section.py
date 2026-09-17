from sqlalchemy import Column, Integer, String, Boolean, DateTime, func
from sqlalchemy.orm import relationship
from app.db.base import Base


class MenuSection(Base):
    __tablename__ = "menu_sections"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, nullable=False)
    description = Column(String, nullable=True)
    image_url = Column(String, nullable=True)
    sort_order = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    # Ordered here rather than at each call site: the public menu renders a
    # section's products straight off this relationship, which had no ordering
    # of its own, so products within a section came back in whatever order the
    # database happened to return them. id breaks ties between products that
    # share a sort_order.
    products = relationship(
        "Product",
        back_populates="section",
        lazy="joined",
        order_by="(Product.sort_order, Product.id)",
    )
