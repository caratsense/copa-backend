"""
The prices the client sent on 26 October, and the Diwali move. Inspect by
default; --apply writes.

THREE THINGS:

1. Seventeen products that have shown "Price coming soon" since September are
   priced and put on sale. Each one drops the draft-no-price tag — which is
   what `is_placeholder` reads, and what the publish guard refuses — and
   becomes available.

2. The cookie boxes are sold in two weights at unrelated prices (300gm 400,
   150gm 200), so they get ProductOptions with explicit prices rather than a
   per-kg base and a multiplier. A product with options is priced ONLY from
   them, which is what stops the global size ladder offering a cookie box in
   2kg. "Jumbo" comes in 300gm only, which is one option, not a smaller menu.

3. "Diwali special will be in gifting section, not seasonal" — the seven
   festive boxes move from Seasonal to Gifting Collection. Nothing about them
   changes but their category: same prices, same photographs (the frontend
   keys those on the product name), same contents.

Re-running is safe: a product already at the right price is left alone, and
options are matched by label so a second run does not duplicate them.

    python -m scripts.price_the_catalogue            # inspect
    python -m scripts.price_the_catalogue --apply    # write

On Railway:
    railway ssh --service copa-backend "python -m scripts.price_the_catalogue --apply"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy.orm.attributes import flag_modified

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import PLACEHOLDER_PRICE_TAG, Product
from app.models.product_option import ProductOption

# name -> (price, pricing_unit)
#
# "kg" means base_price is per kilo and the size ladder multiplies it; "fixed"
# means base_price is the price of the thing itself. The client gave the unit
# for most of these ("1800/kg", "350/pc"); the choux and the eclairs came
# without one and were confirmed as a box price.
SIMPLE_PRICES = {
    # Desserts & Pudding Tubs
    "Classic Fruit Cream Tub": (1800.0, "kg"),
    "Chocolate Coffee Baileys Mousse Cake Tub": (2400.0, "kg"),
    "Banoffee Tub": (2200.0, "kg"),
    "Classic Apple Pie": (350.0, "fixed"),
    "Coffee Chocolate Hazelnut Choux Au Craquelin": (1850.0, "fixed"),
    "Classic Vanilla Bean Chocolate Eclair": (1700.0, "fixed"),
    # Breads & Bun Collection
    "Wheat Bread": (75.0, "fixed"),
    "100% Wheat Bread": (80.0, "fixed"),
    "Burger Bun": (55.0, "fixed"),
    "Pav": (35.0, "fixed"),
    # Cookies — the two sold by the pack. Named AFTER the rename below, which
    # is why renames run first: the client's price for "chocolate brookie" is
    # for the row the catalogue still called "Chocolate Cookie".
    "Chocolate Brookie": (650.0, "fixed"),
    "Biscoff Cookie": (650.0, "fixed"),
}

# Sold in sixes. The site already renders this tag as a "Pack of 6" badge, so
# the quantity is on the card rather than hidden in the price.
PACK_OF_SIX = ("Chocolate Brookie", "Biscoff Cookie")

# name -> list of (label, price). The product's own base_price is set to the
# cheapest option so a menu card showing "from" is honest, but the options are
# what any order is priced from.
OPTION_PRICES = {
    "Chip Chocolate Cookie Box - Button": [("300gm", 400.0), ("150gm", 200.0)],
    "Orange Cardamom Butter Cookie - Button": [("300gm", 400.0), ("150gm", 200.0)],
    # Jumbo is made in one size only — one option, not a missing one.
    "Chip Chocolate Cookie Box - Jumbo": [("300gm", 400.0)],
    "Orange Cardamom Butter Cookie - Jumbo": [("300gm", 400.0)],
    "Sourdough Crackers Jar": [("300gm", 350.0)],
}

# The catalogue calls it "Chocolate Cookie"; the client says the thing in that
# photograph is a brookie, sold in sixes. Renamed rather than added, so the
# row keeps its id, its section and any history attached to it.
RENAMES = {
    "Chocolate Cookie": "Chocolate Brookie",
}

# The festive boxes, and where they belong now.
FESTIVE = [
    "Jack Of All Treats", "Full House", "Shahi Tukda Tres Leches",
    "Matilda Cake", "Jackpot", "Queen Of Hearts", "A Sweet Deal",
]
FESTIVE_FROM = "Seasonal"
FESTIVE_TO = "Gifting Collection"


_OLD_NAME = {new: old for old, new in RENAMES.items()}


def _find(db, name: str) -> Product | None:
    """
    The product called `name`, whether or not the rename has happened yet.

    Inspecting writes nothing, so a product due to be renamed is still under
    its old name when the price lookup reaches it. Without this the dry run
    reported "no product named 'Chocolate Brookie'" and refused — which is a
    lie about what applying would do, and the dry run is the thing a person
    reads before letting it touch the catalogue.
    """
    row = db.query(Product).filter(Product.name == name).first()
    if row is not None:
        return row
    old = _OLD_NAME.get(name)
    return db.query(Product).filter(Product.name == old).first() if old else None


def _clear_placeholder(product: Product) -> None:
    """Drop the tag that marks a price as a stand-in."""
    tags = [t for t in (product.tags or []) if t != PLACEHOLDER_PRICE_TAG]
    product.tags = tags
    flag_modified(product, "tags")


def main() -> int:
    apply_it = "--apply" in sys.argv
    problems = []
    db = SessionLocal()
    try:
        # ─── 1. RENAMES, FIRST: everything below looks products up by name ───
        print("renames")
        for old, new in RENAMES.items():
            row = db.query(Product).filter(Product.name == old).first()
            if row is None:
                if db.query(Product).filter(Product.name == new).first():
                    print(f"  already renamed  {old} -> {new}")
                else:
                    problems.append(f"no product named {old!r} to rename")
                continue
            print(f"  {'RENAME' if apply_it else 'would rename'} {old} -> {new}")
            if apply_it:
                row.name = new
                # Flushed so the price and option lookups below, which go by
                # name, find the row under its NEW name. Without this the
                # script renames Chocolate Cookie and then cannot find
                # Chocolate Brookie to price — which is how it aborted the
                # first time this ran.
                db.flush()

        # ─── 2. SIMPLE PRICES ───
        print("\nprices")
        for name, (price, unit) in SIMPLE_PRICES.items():
            row = _find(db, name)
            if row is None:
                problems.append(f"no product named {name!r}")
                continue
            if row.base_price == price and row.pricing_unit == unit and row.is_available:
                print(f"  already priced   {name} — Rs {price:.0f}/{unit}")
                continue
            print(f"  {'PRICE ' if apply_it else 'would price'} {name} — "
                  f"Rs {price:.0f} ({unit}), was Rs {row.base_price:.0f}")
            if apply_it:
                row.base_price = price
                row.pricing_unit = unit
                row.is_available = True
                _clear_placeholder(row)
                if name in PACK_OF_SIX and "pack-of-6" not in (row.tags or []):
                    row.tags = [*(row.tags or []), "pack-of-6"]
                    flag_modified(row, "tags")

        # ─── 3. THE ONES SOLD IN WEIGHTS ───
        print("\nsizes")
        for name, options in OPTION_PRICES.items():
            row = _find(db, name)
            if row is None:
                problems.append(f"no product named {name!r}")
                continue
            cheapest = min(p for _, p in options)
            existing = {o.label: o for o in row.options}
            for order, (label, price) in enumerate(options, start=1):
                current = existing.get(label)
                if current is not None and current.price == price and current.is_active:
                    print(f"  already set      {name} — {label} Rs {price:.0f}")
                    continue
                print(f"  {'SIZE  ' if apply_it else 'would set'} {name} — "
                      f"{label} Rs {price:.0f}")
                if apply_it:
                    if current is None:
                        db.add(ProductOption(
                            product_id=row.id, label=label, price=price,
                            multiplier=None, sort_order=order, is_active=True,
                        ))
                    else:
                        current.price = price
                        current.multiplier = None
                        current.is_active = True
                        current.sort_order = order
            if apply_it:
                # base_price is not what an order is charged for a product with
                # options, but it is what a card shows before one is chosen, so
                # it is the cheapest rather than a leftover placeholder Rs 1.
                row.base_price = cheapest
                row.pricing_unit = "fixed"
                row.is_available = True
                _clear_placeholder(row)

        # ─── 4. THE DIWALI BOXES MOVE ───
        print(f"\n{FESTIVE_FROM} -> {FESTIVE_TO}")
        target = db.query(MenuSection).filter(MenuSection.name == FESTIVE_TO).first()
        if target is None:
            problems.append(f"no {FESTIVE_TO!r} category to move the festive boxes into")
        else:
            for name in FESTIVE:
                row = _find(db, name)
                if row is None:
                    problems.append(f"no product named {name!r} to move")
                    continue
                if row.section_id == target.id:
                    print(f"  already there    {name}")
                    continue
                where = row.section.name if row.section else "no category"
                print(f"  {'MOVE  ' if apply_it else 'would move'} {name} "
                      f"(from {where})")
                if apply_it:
                    row.section_id = target.id
                    row.category = FESTIVE_TO.lower()

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

        # ─── VERIFY ───
        db.expire_all()
        still_unpriced = [
            p.name for p in db.query(Product).all()
            if p.is_placeholder and not p.is_pricing_row
        ]
        print(f"  products still waiting on a price: {len(still_unpriced)}")
        for n in still_unpriced:
            print(f"    {n}")

        for section_name in (FESTIVE_FROM, FESTIVE_TO):
            sec = db.query(MenuSection).filter(MenuSection.name == section_name).first()
            if sec:
                print(f"  {section_name}: {len(sec.products)} product(s)")

        # A product with options must have at least one, or it cannot be
        # ordered in any size at all.
        for name in OPTION_PRICES:
            row = _find(db, name)
            active = [o for o in row.options if o.is_active] if row else []
            if not active:
                print(f"  VERIFICATION FAILED: {name} has no sizes to order")
                return 1
            print(f"  {name}: {', '.join(f'{o.label} Rs {o.price:.0f}' for o in active)}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
