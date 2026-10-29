"""
A one-rupee cake, for testing a real payment end to end.

The point is to put an actual card through the actual gateway without it
costing anything, so every step after payment — the callback, the status
change, the baker assignment, the WhatsApp message — is exercised for real
rather than simulated.

WHY IT CANNOT BE BOUGHT BY ACCIDENT. It lives in a section with
is_active=False, and that is load-bearing in three places:

  * /menu/sections filters on MenuSection.is_active, so the category and
    everything in it is absent from the menu.
  * The menu page's second call (/products) only merges rows back in for
    sections that call already returned, and only placeholder-priced ones at
    that — this cake is neither.
  * The WhatsApp bot's _catalogue skips products whose section is inactive.

So it is reachable only by its direct URL, which this script prints. A one
rupee cake sitting in a live category would be a genuinely bad day.

    python -m scripts.internal_test_cake            # inspect
    python -m scripts.internal_test_cake --apply    # create it
    python -m scripts.internal_test_cake --remove   # take it away afterwards

On Railway:
    railway ssh --service copa-backend "python -m scripts.internal_test_cake --apply"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import Product

SECTION_NAME = "Internal Testing"
PRODUCT_NAME = "Internal Test Cake"
PRICE = 1.0
SITE = "https://cakeoclock.co.in"


def main() -> int:
    apply_it = "--apply" in sys.argv
    remove_it = "--remove" in sys.argv
    db = SessionLocal()
    try:
        section = db.query(MenuSection).filter(MenuSection.name == SECTION_NAME).first()
        product = db.query(Product).filter(Product.name == PRODUCT_NAME).first()

        # ─── REMOVE ───
        if remove_it:
            if product is None and section is None:
                print("nothing to remove")
                return 0
            print(f"{'REMOVE' if apply_it else 'would remove'} "
                  f"product={product.id if product else '-'} "
                  f"section={section.id if section else '-'}")
            if apply_it:
                if product is not None:
                    db.delete(product)
                if section is not None and not section.products:
                    db.delete(section)
                db.commit()
                print("removed")
            else:
                print("\nInspection only - add --apply to remove.")
            return 0

        # ─── CREATE ───
        if section is None:
            print(f"{'CREATE' if apply_it else 'would create'} section {SECTION_NAME!r} (hidden)")
            if apply_it:
                section = MenuSection(
                    name=SECTION_NAME,
                    description="Not a real category. Holds the rupee cake used to test payments.",
                    sort_order=99,
                    is_active=False,          # the whole safety mechanism
                )
                db.add(section)
                db.flush()
        else:
            print(f"  section exists  id={section.id} active={section.is_active}")
            # If somebody switched it on, switch it back: everything about this
            # depends on the category staying invisible.
            if section.is_active:
                print("  WARNING: the section is ACTIVE — the test cake would be "
                      "on the public menu. Setting it back to hidden.")
                if apply_it:
                    section.is_active = False

        if product is None:
            print(f"{'CREATE' if apply_it else 'would create'} product {PRODUCT_NAME!r} at Rs {PRICE:.0f}")
            if apply_it:
                product = Product(
                    name=PRODUCT_NAME,
                    category="internal-testing",
                    description=("Internal use only. A one rupee line item for putting a "
                                 "real payment through the real gateway."),
                    base_price=PRICE,
                    # Fixed, not per kg: a size multiplier would turn one rupee
                    # into two and the point is to charge exactly one.
                    pricing_unit="fixed",
                    is_customizable=False,
                    is_available=True,
                    section_id=section.id,
                    sort_order=0,
                    tags=["internal"],
                )
                db.add(product)
        else:
            print(f"  product exists  id={product.id} Rs {product.base_price:.0f} "
                  f"unit={product.pricing_unit} available={product.is_available}")
            if apply_it:
                product.base_price = PRICE
                product.pricing_unit = "fixed"
                product.is_available = True
                product.section_id = section.id

        if not apply_it:
            db.rollback()
            print("\nInspection only - nothing was written. Re-run with --apply.")
            return 0

        db.commit()
        db.refresh(product)

        # ─── VERIFY ───
        db.expire_all()
        section = db.query(MenuSection).filter(MenuSection.id == product.section_id).first()
        ok = (
            product.base_price == PRICE
            and product.pricing_unit == "fixed"
            and product.is_available
            and section is not None
            and not section.is_active
        )
        print("\ncommitted")
        print(f"  product id      : {product.id}")
        print(f"  price           : Rs {product.base_price:.0f} ({product.pricing_unit})")
        print(f"  section         : {section.name} (active={section.is_active})")
        print(f"  orderable at    : {SITE}/menu/{product.id}")
        print(f"  on the menu     : no — the category is hidden")
        if not ok:
            print("\nVERIFICATION FAILED: the cake is not in the shape it should be.")
            return 1
        print("\nDelivery is charged on top, so pick Self Pickup for a total of "
              "exactly Rs 1. Run with --remove when you are done.")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
