"""
The sponge prices the client sent on 6 November. Inspect by default; --apply
writes.

WHAT CHANGED
------------
Three numbers, from the WhatsApp thread:

    "Make it 2250/- for classic"        -> the vanilla sponge
    "For chocolate - classic -2300/-"   -> the chocolate sponge
    "Belgian chocolate +150/-"          -> was +200

THE TWO PREMIUM ROWS MOVE WITH THEM
-----------------------------------
A premium cake is the base sponge plus its premium flavour's surcharge, and
the premium pricing rows exist to say that same number a second time - which
is why /build-a-cake/options refuses to offer them as sponges. The client did
not quote them, because from the kitchen's side they are not separate prices;
they are the sum. So they are recomputed rather than left behind:

    Vanilla Premium   = 2250 + 250 (vanilla premium flavour)  = 2500
    Chocolate Premium = 2300 + 150 (Belgian flavour)          = 2450

Leaving them at 2250 and 2400 would mean a customer could reach the same cake
at two different prices, and the vanilla premium row would undercut the plain
vanilla sponge it is built on.

WHAT THIS DOES NOT TOUCH
------------------------
* The catalogue's own cakes. "Belgian Chocolate Cake" and the rest are
  finished products with their own prices, not the builder's +150 surcharge.
* The vanilla premium surcharge, which the client's menu still shows at +250.
* Any existing order. An order stores what it was charged; the rules are only
  consulted when pricing a new one.

Re-running is safe: a row already at the new price is reported and left alone.

    python -m scripts.reprice_sponges_nov            # inspect
    python -m scripts.reprice_sponges_nov --apply    # write

On Railway:
    railway ssh --service copa-backend "python -m scripts.reprice_sponges_nov --apply"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.pricing import FlavorRule
from app.models.product import Product

# The per-kg price of each pricing row, after this change.
SPONGE_PRICES = {
    "Vanilla Base Cake": 2250.0,
    "Chocolate Base Cake": 2300.0,
    "Vanilla Premium Cake": 2500.0,
    "Chocolate Premium Cake": 2450.0,
}

# The Belgian surcharge, and exactly which flavours carry it. Listed by name
# rather than matched on "Belgian", so a flavour the client adds later is
# repriced deliberately instead of by a substring that happens to match.
BELGIAN_SURCHARGE = 150.0
BELGIAN_FLAVOURS = (
    "Plain Belgian Chocolate",
    "Belgian Chocolate Irish Cream",
    "Belgian Chocolate Cookie Cream",
    "Belgian Chocolate Orange",
    "Belgian Chocolate Hazelnut",
    "Belgian Chocolate Pineapple",
    "Belgian Chocolate Butterscotch",
    "Belgian Chocolate Strawberry",
    "Belgian Chocolate Mango",
    "Belgian Chocolate Mango Crumble",
    "Belgian Chocolate Coffee Salted Caramel",
    "Belgian Chocolate Orange Hazelnut Crumble",
)


def main() -> int:
    apply_it = "--apply" in sys.argv
    problems = []
    db = SessionLocal()
    try:
        print("sponge prices")
        for name, price in SPONGE_PRICES.items():
            row = db.query(Product).filter(Product.name == name).first()
            if row is None:
                problems.append(f"no product named {name!r}")
                continue
            if row.base_price == price:
                print(f"  already priced   {name} — Rs {price:.0f}/kg")
                continue
            print(f"  {'PRICE ' if apply_it else 'would price'} {name} — "
                  f"Rs {row.base_price:.0f} -> Rs {price:.0f} per kg")
            if apply_it:
                row.base_price = price

        print("\nBelgian surcharge")
        for name in BELGIAN_FLAVOURS:
            row = db.query(FlavorRule).filter(FlavorRule.name == name).first()
            if row is None:
                problems.append(f"no flavour rule named {name!r}")
                continue
            if row.extra_cost == BELGIAN_SURCHARGE:
                print(f"  already set      {name} — +Rs {BELGIAN_SURCHARGE:.0f}")
                continue
            print(f"  {'SET   ' if apply_it else 'would set'} {name} — "
                  f"+Rs {row.extra_cost:.0f} -> +Rs {BELGIAN_SURCHARGE:.0f}")
            if apply_it:
                row.extra_cost = BELGIAN_SURCHARGE

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
        # The builder prices from the two base rows, so a sponge that is
        # missing or off sale after this would take the whole custom-cake flow
        # down. Read back rather than assumed.
        db.expire_all()
        for name, price in SPONGE_PRICES.items():
            row = db.query(Product).filter(Product.name == name).first()
            if row is None or row.base_price != price:
                print(f"  VERIFICATION FAILED: {name} is not at Rs {price:.0f}")
                return 1
            print(f"  {name}: Rs {row.base_price:.0f}/kg, "
                  f"{'on sale' if row.is_available else 'NOT AVAILABLE'}")

        wrong = [
            r.name for r in db.query(FlavorRule)
            .filter(FlavorRule.name.in_(BELGIAN_FLAVOURS)).all()
            if r.extra_cost != BELGIAN_SURCHARGE
        ]
        if wrong:
            print(f"  VERIFICATION FAILED: still not at +Rs {BELGIAN_SURCHARGE:.0f}: "
                  f"{', '.join(wrong)}")
            return 1
        print(f"  {len(BELGIAN_FLAVOURS)} Belgian flavours at +Rs {BELGIAN_SURCHARGE:.0f}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
