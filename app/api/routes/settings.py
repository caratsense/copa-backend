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


# The only settings an unauthenticated caller may read.
#
# /settings/public returned every row in the table. That table holds
# `brand_phone` — the owner's personal number, the one the WhatsApp bot was
# deliberately changed to withhold unless a message earns it — along with
# `brand_owner` and the payment rules. All of it was one unauthenticated GET
# away, while the frontend reads exactly two of these keys.
#
# An allow-list rather than a deny-list, so a setting added next month is
# private until somebody decides otherwise.
PUBLIC_KEYS = frozenset({
    "active_theme",
    "announcement",
    "announcement_enabled",
    "store_open",
    "store_hours_open",
    "store_hours_close",
    "brand_name",
    "brand_tagline",
    # Where to collect a self-pickup order. Public by design: the customer who
    # chose to collect has to be told where to come, on the order page and in
    # their WhatsApp confirmation. Unlike brand_phone this is a shop front, not
    # a private line.
    "pickup_address",
    # Whether the shop is delivering at all. "false" means pickup only: the
    # checkout reads it to grey the Delivery option out, and POST /orders
    # refuses a delivery order so a stale page cannot place one anyway.
    # Missing means delivering - that is how the shop behaved before the
    # switch existed, so an upgrade changes nothing until an admin flips it.
    "delivery_enabled",
})


# ─── PUBLIC ───────────────────────────────────────────

@router.get("/store-hours")
def get_store_hours(db: Session = Depends(get_db)):
    """
    Public — frontend calls this to show:
    - Whether store is currently open
    - Opening/closing times
    - Message for the customer
    - The earliest date an order may be placed for

    That last one is here, on a call the checkout already makes, rather than
    as a setting the page has to read and interpret for itself. The checkout
    was computing its own earliest date from the browser's clock, so when the
    kitchen closed same-day ordering the date picker carried on offering
    today: the customer chose it, filled the form in, picked a slot and was
    refused on submit. The bot had the right answer because it asks the
    server. Now both ask the same question of the same function.
    """
    from app.services.store_hours import earliest_delivery_time

    hours = dict(is_store_open(db))
    hours["earliest_date"] = earliest_delivery_time(db=db).date().isoformat()
    return hours


@router.get("/public", response_model=list[SiteSettingRead])
def get_public_settings(db: Session = Depends(get_db)):
    """
    Public — what the website needs before anyone has signed in: the theme and
    the announcement banner. See PUBLIC_KEYS for the whole list, and for why
    it is a list rather than everything in the table.
    """
    return (
        db.query(SiteSettings)
        .filter(SiteSettings.key.in_(PUBLIC_KEYS))
        .all()
    )


@router.get("/public/{key}", response_model=SiteSettingRead)
def get_setting(key: str, db: Session = Depends(get_db)):
    """
    One public setting by key.

    A key that is not public answers exactly as a key that does not exist
    does, so this cannot be used to find out which settings are configured.
    """
    setting = (
        db.query(SiteSettings).filter(SiteSettings.key == key).first()
        if key in PUBLIC_KEYS else None
    )
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
