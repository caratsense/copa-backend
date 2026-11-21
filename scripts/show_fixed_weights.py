"""
Put the weight of a fixed-price product on its page. Inspect by default.

WHY THIS EXISTS
---------------
scripts/client_catalogue.py has carried FIXED_WEIGHTS since the menu was first
written down: the three products whose single price is the price of one
specific weight the client quoted. The comment there said the weight was
"recorded so it is not lost from the menu copy" — and it was never lost, but it
was never shown either. Nothing read the dict.

So a customer looking at "Whole Wheat Dates & Walnut Tea Cake — ₹1,700" had no
way to tell how big it was. A per-kg cake answers that with its size buttons.
A fixed-price item has no buttons, because it is one thing at one price, and
the page said nothing at all.

This writes the weight onto the product as a tag, which the menu and the
product page already know how to show: see productMeta() in the frontend's
src/lib/product-meta.ts, which renders a "weight-800g" tag as an "800 g" badge
beside "Pack of 6" and "Eggless". A tag is how this catalogue already says
"true of the product, not of the menu".

NOT A SIZE
----------
The tag is a label, nothing more. It does not become a ProductOption, so it
adds no button, asks the customer to choose nothing, and never reaches an
order line. The price and the pricing_unit are not touched.

    python -m scripts.show_fixed_weights                     # inspect
    python -m scripts.show_fixed_weights --apply             # write
    python -m scripts.show_fixed_weights --only "Whole Wheat Dates & Walnut Tea Cake" --apply

--only limits the run to one product, by exact name. Two of the three already
carry their weight in their own name ("Dog Cake - 500gm"), where a badge would
simply repeat it, so which of them is worth tagging is the client's call
rather than this script's.

Idempotent: a product that already carries its weight tag is left alone, and a
product carrying a DIFFERENT weight tag has it replaced, so a corrected weight
in FIXED_WEIGHTS does not accumulate a second badge.

On Railway:
    railway ssh --service copa-backend "python -m scripts.show_fixed_weights --apply"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.product import Product
from scripts.client_catalogue import FIXED_WEIGHTS, TAG_FORMAT

WEIGHT_PREFIX = "weight-"


def weight_tag(weight: str) -> str:
    """
    "800 g" -> "weight-800g", "1.5 kg" -> "weight-1-5kg".

    The hyphen stands in for the decimal point: a product tag is
    lower-case-kebab (TAG_FORMAT) and cannot hold a dot. The frontend reads it
    back the same way, so the badge still says "1.5 kg".
    """
    tag = WEIGHT_PREFIX + weight.lower().replace(" ", "").replace(".", "-")
    if not TAG_FORMAT.match(tag):
        raise ValueError(f"{weight!r} does not make a usable tag ({tag!r})")
    return tag


def main() -> int:
    apply_it = "--apply" in sys.argv
    only = None
    if "--only" in sys.argv:
        at = sys.argv.index("--only")
        if at + 1 >= len(sys.argv):
            print("--only needs a product name.")
            return 1
        only = sys.argv[at + 1]

    wanted = dict(FIXED_WEIGHTS)
    if only is not None:
        if only not in wanted:
            print(f"{only!r} is not in FIXED_WEIGHTS. It holds:")
            for name in FIXED_WEIGHTS:
                print(f"  - {name}")
            return 1
        wanted = {only: wanted[only]}

    db = SessionLocal()
    try:
        planned = []
        missing = []
        for name, weight in wanted.items():
            tag = weight_tag(weight)
            product = db.query(Product).filter(Product.name == name).first()
            if product is None:
                missing.append(name)
                print(f"  NOT FOUND  {name}")
                continue

            tags = list(product.tags or [])
            stale = [t for t in tags if t.startswith(WEIGHT_PREFIX) and t != tag]
            if tag in tags and not stale:
                print(f"  ok         {name} (id {product.id}) already shows {weight}")
                continue

            kept = [t for t in tags if not t.startswith(WEIGHT_PREFIX)]
            was = f" (was {', '.join(stale)})" if stale else ""
            print(f"  {'TAG    ' if apply_it else 'would tag'}  {name} (id {product.id}): "
                  f"+ {tag}{was}")
            planned.append((product, kept + [tag]))

        if missing:
            # A name that matches nothing means FIXED_WEIGHTS and the database
            # have drifted apart. Tagging the rest would hide that.
            print(f"\nABORTED: {len(missing)} product(s) in FIXED_WEIGHTS are not in "
                  f"this database. Reconcile the catalogue first.")
            return 1

        if not planned:
            print("\nNothing to change.")
            return 0

        if not apply_it:
            print("\nInspection only - nothing was written. Re-run with --apply.")
            return 0

        for product, tags in planned:
            # Assigned, not appended: tags is JSONB, and SQLAlchemy does not
            # see a list mutated in place.
            product.tags = tags
        db.commit()
        print("\ncommitted")

        db.expire_all()
        for name in wanted:
            product = db.query(Product).filter(Product.name == name).first()
            shown = [t for t in (product.tags or []) if t.startswith(WEIGHT_PREFIX)]
            print(f"  {name}: {shown or 'no weight'}")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
