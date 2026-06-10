from .base import Base
from .session import get_db, engine, SessionLocal

# Import all models so Base.metadata knows about every table.
# When you add a new model file, add its import here.
from app.models.user import User  # noqa
from app.models.menu_section import MenuSection  # noqa
from app.models.product import Product  # noqa
from app.models.pricing import SizeRule, FlavorRule, DesignRule, AddonRule, RushRule  # noqa
from app.models.delivery import DeliveryZone  # noqa
from app.models.order import Order  # noqa
from app.models.order_item import OrderItem  # noqa
from app.models.event import OrderEvent  # noqa
from app.models.coupon import Coupon  # noqa
from app.models.ingredient import Ingredient  # noqa
from app.models.review import Review  # noqa
from app.models.site_settings import SiteSettings  # noqa
from app.models.trusted_device import TrustedDevice  # noqa
from app.models.address import Address  # noqa
from app.models.extra import Extra  # noqa

__all__ = ["Base", "get_db", "engine", "SessionLocal"]
