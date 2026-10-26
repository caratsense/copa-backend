"""
The festive hampers, into the Seasonal category. Inspect by default; --apply writes.

Transcribed from the client's own cards (October 2026): seven products, each a
fixed-price box rather than a cake sold by the kilo, so each is created with
pricing_unit="fixed" and the global size ladder never applies to it.

What lives WHERE, and why:

  here      name, price, the one-line description from the card, and the tags
            the site renders as badges
  frontend  the contents table, storage, serving and shelf life
            (src/lib/seasonal-hampers.ts)

The contents are a table — item, quantity, best-within — and `description` is a
single string. Flattening a table into one line to fit the column would lose
the quantities, which are the thing a customer buying a hamper is actually
comparing. They are presentation, they change with the season, and they do not
belong in the pricing path.

Re-running is safe: a hamper that already exists is left alone, including its
price, because the client may have edited it in the admin screen since.

    python -m scripts.setup_seasonal_hampers            # inspect
    python -m scripts.setup_seasonal_hampers --apply    # write

On Railway:
    railway ssh --service copa-backend "python -m scripts.setup_seasonal_hampers --apply"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import Product

SECTION_NAME = "Seasonal"

# (name, price, the card's own one-liner, tags)
#
# "eggless" is on every one because every card carries the 100% EGGLESS mark.
# "hamper" marks the boxes that hold several things, as against the two that
# are a single sweet; the site shows neither as a badge today, but the tag is
# what a later "gift hampers" filter would read.
HAMPERS = [
    ("Jack Of All Treats", 2899.0, "Master of every craving.", ["eggless", "hamper", "festive"]),
    ("Full House", 2499.0, "A full house of indulgence.", ["eggless", "hamper", "festive"]),
    ("Shahi Tukda Tres Leches", 2450.0, "An ode to India's rich culture.", ["eggless", "festive"]),
    ("Matilda Cake", 2450.0, "Pure chocolate indulgence.", ["eggless", "festive"]),
    ("Jackpot", 1899.0, "The sweet win of the season.", ["eggless", "hamper", "festive"]),
    ("Queen Of Hearts", 1799.0, "Winning hearts, one bite at a time.", ["eggless", "hamper", "festive"]),
    ("A Sweet Deal", 1699.0, "Your winning hand in all things sweet.", ["eggless", "hamper", "festive"]),
]

# "Matilda Cake" is already in the catalogue — an unpriced placeholder in
# Desserts & Pudding Tubs, waiting on exactly the price this card supplies. So
# that row is MOVED and priced rather than duplicated: two products of the same
# name, one buyable and one "coming soon", is the worse outcome for everybody,
# and a second row would not be the same cake to the kitchen either.
#
# Only an unpriced placeholder is treated this way. A row that already has a
# real price is a decision about which one the client meant, so the script
# stops and says so instead of overwriting a price.
MOVE_IF_PLACEHOLDER = True


def main() -> int:
    apply_it = "--apply" in sys.argv
    problems = []
    db = SessionLocal()
    try:
        section = db.query(MenuSection).filter(MenuSection.name == SECTION_NAME).first()
        if section is None:
            print(f"ABORTED: no {SECTION_NAME!r} category. "
                  f"Run scripts.setup_menu_meeting_oct first.")
            return 1

        print(f"{SECTION_NAME} (id {section.id}) — "
              f"{len(section.products)} product(s) today")
        print()

        for order, (name, price, blurb, tags) in enumerate(HAMPERS, start=1):
            existing = db.query(Product).filter(Product.name == name).all()
            in_section = [p for p in existing if p.section_id == section.id]
            elsewhere = [p for p in existing if p.section_id != section.id]

            if in_section:
                p = in_section[0]
                print(f"  already there  {name} (id {p.id}, ₹{p.base_price:.0f})")
                continue
            if elsewhere:
                p = elsewhere[0]
                where = p.section.name if p.section else "no category"
                if not (MOVE_IF_PLACEHOLDER and p.is_placeholder):
                    problems.append(
                        f"{name!r} already exists in {where} (id {p.id}) at "
                        f"₹{p.base_price:.0f} — decide which row the festive box is"
                    )
                    print(f"  CLASH          {name} — priced row in {where} (id {p.id})")
                    continue
                print(f"  {'MOVE  ' if apply_it else 'would move'} {name} (id {p.id}) "
                      f"from {where}, and price it ₹{price:.0f}")
                if apply_it:
                    p.section_id = section.id
                    p.category = SECTION_NAME.lower()
                    p.base_price = price
                    p.pricing_unit = "fixed"
                    p.description = blurb
                    p.sort_order = order
                    # A festive box is not configured by the customer: it is
                    # what the card says it is. Left as it was, the moved row
                    # kept the catalogue default and offered "Customize" where
                    # its six neighbours offer "View".
                    p.is_customizable = False
                    # Dropping the placeholder tag is what makes it sellable:
                    # is_placeholder is read off the tags, and the product API
                    # refuses to publish a row that still carries it.
                    p.tags = list(tags)
                    p.is_available = True
                    from sqlalchemy.orm.attributes import flag_modified
                    flag_modified(p, "tags")
                continue

            print(f"  {'ADD   ' if apply_it else 'would add'} {name} — ₹{price:.0f} "
                  f"(fixed price, sort {order})")
            if apply_it:
                db.add(Product(
                    name=name,
                    category=SECTION_NAME.lower(),
                    description=blurb,
                    base_price=price,
                    # A box, not a cake by the kilo: the size ladder and its
                    # multipliers must never be applied to this price.
                    pricing_unit="fixed",
                    is_customizable=False,
                    is_available=True,
                    tags=list(tags),
                    section_id=section.id,
                    sort_order=order,
                ))

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

        db.expire_all()
        section = db.query(MenuSection).filter(MenuSection.name == SECTION_NAME).first()
        rows = sorted(section.products, key=lambda p: p.sort_order)
        print(f"  {SECTION_NAME}: {len(rows)} product(s)")
        for p in rows:
            print(f"    {p.name:26} ₹{p.base_price:>7.0f}  {p.pricing_unit:5}  "
                  f"available: {p.is_available}")
        # Every one of these must be buyable, or the category looks stocked and
        # sells nothing.
        unavailable = [p.name for p in rows if not p.is_available]
        if unavailable:
            print(f"  VERIFICATION FAILED: not available: {unavailable}")
            return 1
        per_kg = [p.name for p in rows if p.pricing_unit != "fixed"]
        if per_kg:
            print(f"  VERIFICATION FAILED: priced by the kilo, so a size "
                  f"multiplier would apply: {per_kg}")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
