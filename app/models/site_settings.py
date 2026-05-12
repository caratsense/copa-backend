from sqlalchemy import Column, Integer, String, DateTime, func
from sqlalchemy.dialects.postgresql import JSONB
from app.db.base import Base


class SiteSettings(Base):
    """
    Key-value store for site-wide settings controlled by admin.
    
    Used for:
    - active_theme: "default", "diwali", "christmas", "valentines", "holi", etc.
    - announcement: banner text shown on the site
    - store_open: whether the store is accepting orders
    - Any other dynamic setting the admin wants to control
    """
    __tablename__ = "site_settings"

    id = Column(Integer, primary_key=True, index=True)
    key = Column(String, unique=True, nullable=False, index=True)
    value = Column(String, nullable=False)
    metadata_json = Column(JSONB, default=dict)    # extra data for complex settings
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())
