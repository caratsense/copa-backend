"""
Going public: hide the testing category, and throw the two shop switches.

WHY A SCRIPT AND NOT A MIGRATION
--------------------------------
Each of these is an ordinary row the admin screen can edit. Applied at boot
they would silently undo whatever Shriya changed that morning, and do it on a
redeploy she did not ask for. So this is a deliberate step somebody takes
once, like scripts/standardise_delivery.py.

WHAT IT DOES
------------
1. Switches the Internal Testing category off. It holds one cake at Rs 1, used
   to exercise payments end to end, and it was showing to customers as a
   thirteenth category. Switched OFF and not deleted: the row stays for
   testing, it simply stops being a category anyone can browse. /menu/sections
   filters on is_active, so this hides it from the website and the bot at once.

2. Delivery off, collection only, until Shriya asks for it back.

3. No same-day orders while the kitchen is swamped. A DATE and not a flag, so
   it expires by itself - see store_hours.SAME_DAY_BLOCK_KEY. The reason for
   setting it expires too, and a flag somebody has to remember to turn off is
   how a shop ends up refusing same-day orders in December because of a
   Tuesday in November.

WHAT IT DELIBERATELY DOES NOT TOUCH
-----------------------------------
Prices. scripts/reprice_sponges_nov.py owns those - it carries the 6 November
numbers AND recomputes the two premium rows that follow from them, which this
script did not. Two scripts writing the same four prices is how they end up
disagreeing.

The other empty categories - Wedding, Specialty, Grazing Tables, Seasonal.
Only what was asked for.

USAGE
-----
    railway ssh --service copa-backend "python -m scripts.go_live_switches"
    railway ssh --service copa-backend "python -m scripts.go_live_switches --apply"

Idempotent: a second run reports nothing to do.
"""

import sys
from datetime import date

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.site_settings import SiteSettings
from app.services.store_hours import DELIVERY_ENABLED_KEY, SAME_DAY_BLOCK_KEY

HIDE_SECTION = "Internal Testing"


def _setting(db, key, value, changes, apply):
    row = db.query(SiteSettings).filter(SiteSettings.key == key).first()
    current = (row.value if row else None)
    if current == value:
        return
    changes.append(f"  SETTING {key:26s} {current!r} -> {value!r}")
    if apply:
        if row:
            row.value = value
        else:
            db.add(SiteSettings(key=key, value=value))


def main() -> int:
    apply = "--apply" in sys.argv
    db = SessionLocal()
    changes: list[str] = []

    try:
        # ── 1. the testing category ──
        section = db.query(MenuSection).filter(MenuSection.name == HIDE_SECTION).first()
        if section is not None and section.is_active:
            changes.append(f"  HIDE    category {HIDE_SECTION!r} (is_active -> False)")
            if apply:
                section.is_active = False

        # ── 2 & 3. the switches ──
        _setting(db, DELIVERY_ENABLED_KEY, "false", changes, apply)
        _setting(db, SAME_DAY_BLOCK_KEY, date.today().isoformat(), changes, apply)

        if not changes:
            print("Catalogue is already as it should be. Nothing to do.")
            return 0

        print(f"{len(changes)} changes:\n")
        print("\n".join(changes))

        if not apply:
            print("\nDRY RUN - nothing was written. Re-run with --apply.")
            return 0

        db.commit()
        print("\nApplied.")

        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
