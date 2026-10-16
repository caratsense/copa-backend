"""
The data behind the October menu changes. Inspect by default; --apply writes.

TWO THINGS, both data rather than behaviour:

1. The four pricing rows
   "Vanilla Base Cake", "Chocolate Base Cake" and the two premium rows are
   prices Build a Cake quotes from, not cakes a customer buys. Listed as
   products they read as a stray first item at the head of their category.
   They are TAGGED here, not deleted and not switched off: /build-a-cake/options
   looks the base rows up by name and refuses to serve the builder at all if
   either is missing or unavailable, so removing them would take the whole
   custom-cake flow down. The tag is what the menu endpoints filter on.

2. Two new categories
   "Seasonal" and "Grazing Tables", both empty. An empty category already
   renders as an enquiry panel on the menu, which is exactly what a grazing
   table needs until there is a price list for one. Created, never deleted, so
   re-running this cannot disturb a section someone has since filled.

Nothing here touches an existing order, a price, or a product's availability.

    python -m scripts.setup_menu_meeting_oct            # inspect
    python -m scripts.setup_menu_meeting_oct --apply    # write

On Railway:
    railway ssh --service copa-backend "python -m scripts.setup_menu_meeting_oct --apply"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm.attributes import flag_modified

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import PRICING_ROW_TAG, Product

# By name, not by id. The ids happen to be 1-4 today, but a name is what the
# client and the kitchen call these rows, and a wrong id would silently tag a
# real cake out of the catalogue.
PRICING_ROW_NAMES = [
    "Vanilla Base Cake",
    "Chocolate Base Cake",
    "Vanilla Premium Cake",
    "Chocolate Premium Cake",
]

# The two base rows the builder cannot work without. Tagging must not make
# either unavailable, so it is checked after the write rather than assumed.
BUILDER_ROWS = ["Vanilla Base Cake", "Chocolate Base Cake"]

# Appended after the existing fifteen rather than slotted among them: the menu
# orders by sort_order, and renumbering sections to make room would move every
# category the client has already seen.
NEW_SECTIONS = [
    ("Seasonal", "What we're baking right now — ask us what's in season."),
    ("Grazing Tables", "A table of cakes, pastries and bakes for an event. "
                       "Quoted individually, so we price it with you."),
]


def main() -> int:
    apply_it = "--apply" in sys.argv
    problems = []
    db = SessionLocal()
    try:
        print("pricing rows")
        for name in PRICING_ROW_NAMES:
            rows = db.query(Product).filter(Product.name == name).all()
            if not rows:
                problems.append(f"no product named {name!r}")
                continue
            if len(rows) > 1:
                problems.append(f"{len(rows)} products named {name!r} - expected one")
                continue
            row = rows[0]
            tags = list(row.tags or [])
            if PRICING_ROW_TAG in tags:
                print(f"  already tagged  {name} (id {row.id})")
                continue
            print(f"  {'TAG   ' if apply_it else 'would tag'} {name} (id {row.id})")
            if apply_it:
                tags.append(PRICING_ROW_TAG)
                row.tags = tags
                # JSONB is mutable and SQLAlchemy does not see an in-place
                # change; without this the write is silently dropped.
                flag_modified(row, "tags")

        print()
        print("sections")
        existing = {s.name: s for s in db.query(MenuSection).all()}
        highest = max([s.sort_order or 0 for s in existing.values()] or [0])
        for offset, (name, description) in enumerate(NEW_SECTIONS, start=1):
            if name in existing:
                print(f"  already there   {name} (id {existing[name].id})")
                continue
            print(f"  {'ADD   ' if apply_it else 'would add'} {name} "
                  f"(sort_order {highest + offset})")
            if apply_it:
                db.add(MenuSection(name=name, description=description,
                                   sort_order=highest + offset, is_active=True))

        if problems:
            db.rollback()
            print("\nABORTED:")
            for p in problems:
                print(f"  - {p}")
            return 1

        if not apply_it:
            db.rollback()
            print("\nInspection only - nothing was written. Re-run with --apply.")
            return 0

        db.commit()
        print("\ncommitted")

        # The builder breaks loudly and completely if either base row stops
        # being available, so prove it did not rather than trusting the write.
        db.expire_all()
        for name in BUILDER_ROWS:
            row = db.query(Product).filter(Product.name == name).first()
            if row is None or not row.is_available:
                print(f"  VERIFICATION FAILED: {name} is missing or unavailable - "
                      f"Build a Cake will not serve")
                return 1
            print(f"  {name}: available, ₹{row.base_price:.0f}, "
                  f"hidden from the catalogue: {row.is_pricing_row}")
        sections = (db.query(MenuSection).filter(MenuSection.is_active == True)
                    .order_by(MenuSection.sort_order).all())
        print(f"  {len(sections)} active sections, ending "
              f"{[s.name for s in sections[-2:]]}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
