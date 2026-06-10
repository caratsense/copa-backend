"""
Site Settings Routes
=====================
Admin controls site-wide settings like:
- Active theme (default, diwali, christmas, valentines, holi)
- Store open/closed
- Announcement banner text
- Any other key-value setting

Frontend fetches GET /settings/public to know the current theme.
"""

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.db import get_db
from app.models.user import User
from app.models.site_settings import SiteSettings
from app.core.auth import require_admin
from app.schemas import SiteSettingRead, SiteSettingUpdate
from app.services.store_hours import is_store_open

router = APIRouter(prefix="/settings", tags=["Site Settings"])


# ─── PUBLIC ───────────────────────────────────────────

@router.get("/store-hours")
def get_store_hours(db: Session = Depends(get_db)):
    """
    Public — frontend calls this to show:
    - Whether store is currently open
    - Opening/closing times
    - Message for the customer
    """
    return is_store_open(db)


@router.get("/public", response_model=list[SiteSettingRead])
def get_public_settings(db: Session = Depends(get_db)):
    """
    Public — frontend fetches this on load to get:
    - active_theme
    - announcement
    - store_open
    """
    return db.query(SiteSettings).all()


@router.get("/public/{key}", response_model=SiteSettingRead)
def get_setting(key: str, db: Session = Depends(get_db)):
    """Get a single setting by key."""
    setting = db.query(SiteSettings).filter(SiteSettings.key == key).first()
    if not setting:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")
    return setting


# ─── ADMIN ────────────────────────────────────────────

@router.get("", response_model=list[SiteSettingRead])
def list_all_settings(admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    """Admin — list all settings."""
    return db.query(SiteSettings).order_by(SiteSettings.key).all()


@router.put("/{key}", response_model=SiteSettingRead)
def upsert_setting(
    key: str,
    data: SiteSettingUpdate,
    admin: User = Depends(require_admin),
    db: Session = Depends(get_db),
):
    """Admin — create or update a setting."""
    setting = db.query(SiteSettings).filter(SiteSettings.key == key).first()

    if setting:
        setting.value = data.value
        if data.metadata_json is not None:
            setting.metadata_json = data.metadata_json
    else:
        setting = SiteSettings(
            key=key,
            value=data.value,
            metadata_json=data.metadata_json or {},
        )
        db.add(setting)

    db.commit()
    db.refresh(setting)
    return setting


@router.delete("/{key}", status_code=204)
def delete_setting(key: str, admin: User = Depends(require_admin), db: Session = Depends(get_db)):
    setting = db.query(SiteSettings).filter(SiteSettings.key == key).first()
    if not setting:
        raise HTTPException(status_code=404, detail=f"Setting '{key}' not found")
    db.delete(setting)
    db.commit()
