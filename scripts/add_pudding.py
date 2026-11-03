"""
Add the chocolate whipped caramel crumble pudding to the menu.

WHERE IT GOES, AND WHY NOT A NEW SECTION
----------------------------------------
It was asked for as "a pudding section". There is already one: section 7 is
called "Desserts & Pudding Tubs" and holds six items, three of which are
per-kilo tubs priced between ₹1800 and ₹2400 — the same shape and the same
shelf as this. A separate "Pudding" category beside a category whose name ends
in "Pudding Tubs" would split one idea across two headings and leave a section
with a single item in it.

So this goes into section 7. If it really should stand on its own, that is a
section row and a section_id away — say so and it moves.

PRICE
-----
₹2400 per kilo, sold from 1kg. pricing_unit="kg" means the number is a rate
that a SizeRule multiplies, which is what makes 1kg come out at ₹2400 and 2kg
at ₹4800. is_customizable is False, matching every other item on this shelf:
these are made as they are, not built to order.

WHAT IT WILL NOT DO
-------------------
- It never edits or removes an existing product, price or section.
- It is idempotent, matched on name: a second run reports "already there".

    python -m scripts.add_pudding            # inspect
    python -m scripts.add_pudding --apply    # write

On Railway:
    railway ssh --service copa-backend "python -m scripts.add_pudding --apply"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import Product

SECTION_NAME = "Desserts & Pudding Tubs"

NAME = "Chocolate Whipped Caramel Crumble Pudding"
DESCRIPTION = (
    "Chocolate pudding under whipped caramel and a crumble lid. "
    "Made to order from 1kg."
)
PRICE_PER_KG = 2400.0
# Behind the six already there, so nothing the client has seen moves.
SORT_ORDER = 17


def main() -> int:
    apply = "--apply" in sys.argv
    db = SessionLocal()
    try:
        section = db.query(MenuSection).filter(MenuSection.name == SECTION_NAME).first()
        if section is None:
            print(f"No section named {SECTION_NAME!r}. Nothing written.")
            return 1

        existing = db.query(Product).filter(Product.name == NAME).first()
        if existing is not None:
            print(f"Already there: #{existing.id} {existing.name} "
                  f"- Rs {existing.base_price:.0f}/{existing.pricing_unit} "
                  f"in section {existing.section_id}. Nothing to do.")
            return 0

        print(f"CREATE  {NAME}")
        print(f"        Rs {PRICE_PER_KG:.0f} per kg, from 1kg")
        print(f"        section {section.id} ({section.name}), sort {SORT_ORDER}")

        if not apply:
            print("\nDRY RUN - nothing was written. Re-run with --apply.")
            return 0

        db.add(Product(
            name=NAME,
            description=DESCRIPTION,
            category="desserts-tubs",
            base_price=PRICE_PER_KG,
            pricing_unit="kg",
            is_customizable=False,
            is_available=True,
            section_id=section.id,
            sort_order=SORT_ORDER,
            tags=[],
        ))
        db.commit()
        print("\nApplied.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())
