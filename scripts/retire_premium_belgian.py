"""
Take the "Premium Belgian" category off the menu. Inspect by default; --apply writes.

Asked for after the October review: the category's only row was "Chocolate
Premium Cake", which is a price Build a Cake quotes from rather than a cake to
buy, so once pricing rows stopped being listed the category stood empty on the
menu — a tab a customer could open to find nothing.

DEACTIVATED, not deleted. is_active=False is what the public menu already
filters on, and the section keeps its id, its name and its products, so this is
one boolean to put back if the client fills it later. Deleting it would orphan
"Chocolate Premium Cake", and that row still has a price the builder reads.

    python -m scripts.retire_premium_belgian            # inspect
    python -m scripts.retire_premium_belgian --apply    # write
    python -m scripts.retire_premium_belgian --restore  # put it back

On Railway:
    railway ssh --service copa-backend "python -m scripts.retire_premium_belgian --apply"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import Product

SECTION_NAME = "Premium Belgian"


def main() -> int:
    apply_it = "--apply" in sys.argv
    restore = "--restore" in sys.argv
    want_active = True if restore else False

    db = SessionLocal()
    try:
        section = db.query(MenuSection).filter(MenuSection.name == SECTION_NAME).first()
        if section is None:
            print(f"No section named {SECTION_NAME!r} — nothing to do.")
            return 0

        products = db.query(Product).filter(Product.section_id == section.id).all()
        print(f"{SECTION_NAME} (id {section.id}) — active: {section.is_active}")
        for p in products:
            kind = "pricing row" if p.is_pricing_row else "product"
            print(f"  holds: {p.name} (id {p.id}, {kind}, ₹{p.base_price:.0f}, "
                  f"available: {p.is_available})")
        if not products:
            print("  holds: nothing")

        # A real product in here would disappear from the menu with the
        # section, which is not what was asked for.
        sellable = [p for p in products if not p.is_pricing_row]
        if sellable and not restore:
            print("\nABORTED: this category still holds products a customer can buy:")
            for p in sellable:
                print(f"  - {p.name} (id {p.id})")
            print("Move them to another category first, or they vanish from the menu.")
            return 1

        if section.is_active == want_active:
            print(f"\nAlready {'active' if want_active else 'off the menu'} — nothing to change.")
            return 0

        verb = "RESTORE" if restore else "HIDE"
        print(f"\n  {verb if (apply_it or restore) else 'would ' + verb.lower()} {SECTION_NAME}")
        if not (apply_it or restore):
            print("\nInspection only - nothing was written. Re-run with --apply.")
            return 0

        section.is_active = want_active
        db.commit()
        print("\ncommitted")

        db.expire_all()
        live = (db.query(MenuSection).filter(MenuSection.is_active == True)
                .order_by(MenuSection.sort_order).all())
        print(f"  {len(live)} categories on the menu")
        print(f"  {SECTION_NAME} is {'on' if want_active else 'off'} it")
        # The row inside it still has to work, because the builder reads it.
        for p in products:
            print(f"  {p.name}: available {p.is_available}, ₹{p.base_price:.0f} (unchanged)")
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
