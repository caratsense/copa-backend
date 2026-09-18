"""
Put products 3 and 4 back in the sections they belong to.

WHAT HAPPENED
-------------
Two of the five original Build-a-Cake tier products were moved in production by
hand, through the admin. As of 17/09 the live database holds:

    section 1  Signature Collection  ->  products 3, 5     (expected 5)
    section 2  Vanilla Cakes         ->  products 1, 4, 2  (expected 1, 2)
    section 3  Chocolate Cakes       ->  (empty)           (expected 3)
    section 4  Premium Belgian       ->  (empty)           (expected 4)

So "Chocolate Base Cake" (id 3) is sitting under Signature Collection, and
"Chocolate Premium Cake" (id 4) under Vanilla Cakes, while the two chocolate
sections stand empty.

Confirmed as a mistake rather than deliberate curation, so this puts them back.

WHY IT MATTERS BEYOND TIDINESS
------------------------------
scripts/setup_menu_sections.py renames section 2 to "Vanilla Celebration
Cakes". Left alone, a chocolate cake goes live in the vanilla section of the
customer-facing menu. That script also refuses to run at all while the baseline
does not match, which is what surfaced this in the first place - so the section
configuration, and the whole catalogue deployment behind it, is blocked until
this is put right.

WHAT IT DOES
------------
1. Reports where products 3 and 4 currently sit.
2. With --apply, sets exactly two fields:
       products.section_id = 3  WHERE id = 3
       products.section_id = 4  WHERE id = 4
3. Re-reads on a fresh connection and checks the result against the BASELINE
   that setup_menu_sections demands - imported from that script rather than
   restated here, so the two cannot disagree about what "correct" means.

WHAT IT WILL NOT DO
-------------------
- It touches no product other than ids 3 and 4, and no column other than
  section_id. No price, name, availability or tag is read or written.
- It creates, renames and deletes nothing.
- It refuses if the database does not look like the situation described above,
  so it cannot be pointed at an unfamiliar state and left to guess.
- It is idempotent. Run it twice and the second run reports "already correct".

ORDER OF OPERATIONS
-------------------
Run this BEFORE scripts/setup_menu_sections.py, while section 3 is still named
"Chocolate Cakes" and section 4 "Premium Belgian". It matches by id, not by
name, so it would still work afterwards - but setup_menu_sections will not run
until this has, which makes the order moot in practice.

USAGE
-----
    python -m scripts.fix_product_sections            # look, change nothing
    python -m scripts.fix_product_sections --apply    # actually move them

On Railway this must run inside the container:

    railway ssh --service copa-backend "python -m scripts.fix_product_sections"
    railway ssh --service copa-backend "python -m scripts.fix_product_sections --apply"

ROLLING BACK
------------
If the moves turn out to have been deliberate after all:

    UPDATE products SET section_id = 1 WHERE id = 3;
    UPDATE products SET section_id = 2 WHERE id = 4;

and update BASELINE in setup_menu_sections.py to match, since that script
encodes the expectation this one restores.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db import SessionLocal
from app.models.menu_section import MenuSection
from app.models.product import Product

# The expectation lives in setup_menu_sections; this script restores it rather
# than holding a second opinion about it.
from scripts.setup_menu_sections import BASELINE, _product_ids


# id -> the section id it belongs in. Only these two products are ever touched.
INTENDED_SECTION = {3: 3, 4: 4}

# Where each one is expected to have been found before the fix. Used only to
# recognise the situation; a row already in its intended section is fine.
DISPLACED_FROM = {3: 1, 4: 2}


class UnexpectedState(Exception):
    """The database does not look like the situation this script is for."""


def _report(db) -> None:
    print("Current section assignments:")
    for section in db.query(MenuSection).order_by(MenuSection.id).all():
        if section.id not in BASELINE:
            continue
        found = _product_ids(db, section.id)
        expected = BASELINE[section.id]["products"]
        flag = "" if found == expected else f"   <-- expected {expected}"
        print(f"  section {section.id}  {section.name:<24} {found}{flag}")


def _check_recognisable(db) -> list[int]:
    """
    Which products still need moving. Raises if anything else is off.

    Being strict here is the point: this script exists to undo one specific
    accident, and a database in some other shape needs a person to look at it,
    not a script that moves two rows and reports success.
    """
    to_move = []
    for product_id, intended in INTENDED_SECTION.items():
        product = db.query(Product).filter(Product.id == product_id).first()
        if product is None:
            raise UnexpectedState(f"product id {product_id} does not exist")
        if product.section_id == intended:
            continue
        if product.section_id != DISPLACED_FROM[product_id]:
            raise UnexpectedState(
                f"product {product_id} ({product.name!r}) is in section "
                f"{product.section_id}, which is neither where it belongs "
                f"({intended}) nor where it was found displaced to "
                f"({DISPLACED_FROM[product_id]})"
            )
        to_move.append(product_id)

    for section_id in INTENDED_SECTION.values():
        if db.query(MenuSection).filter(MenuSection.id == section_id).first() is None:
            raise UnexpectedState(f"section id {section_id} does not exist")

    return to_move


def _verify(db) -> list[str]:
    """Every baseline section holds exactly what setup_menu_sections expects."""
    problems = []
    for section_id, expect in BASELINE.items():
        found = _product_ids(db, section_id)
        if found != expect["products"]:
            problems.append(
                f"section {section_id} holds {found}, expected {expect['products']}"
            )
    return problems


def main() -> int:
    parser = argparse.ArgumentParser(description="Restore products 3 and 4 to their sections.")
    parser.add_argument("--apply", action="store_true",
                        help="perform the move. Without it, nothing is written.")
    args = parser.parse_args()

    db = SessionLocal()
    try:
        _report(db)
        print()

        try:
            to_move = _check_recognisable(db)
        except UnexpectedState as e:
            print(f"ABORTED: {e}", file=sys.stderr)
            print("Nothing was written. This needs a person to look at it.",
                  file=sys.stderr)
            return 2

        if not to_move:
            print("Already correct - both products are in their intended sections.")
            remaining = _verify(db)
            if remaining:
                print()
                print("But the baseline still does not match:")
                for problem in remaining:
                    print(f"  - {problem}")
                print("setup_menu_sections will refuse until that is resolved.")
                return 1
            print("Baseline matches; setup_menu_sections can run.")
            return 0

        for product_id in to_move:
            product = db.query(Product).filter(Product.id == product_id).first()
            print(f"  {'MOVE   ' if args.apply else 'WOULD MOVE'} "
                  f"id {product_id} {product.name!r}: section "
                  f"{product.section_id} -> {INTENDED_SECTION[product_id]}")

        if not args.apply:
            print()
            print("Nothing has been changed. Re-run with --apply to perform the move.")
            return 0

        for product_id in to_move:
            product = db.query(Product).filter(Product.id == product_id).first()
            product.section_id = INTENDED_SECTION[product_id]
        db.commit()
        print()
        print(f"Moved {len(to_move)} product(s).")

        # Fresh read, so verification sees committed state rather than the
        # session's own idea of it.
        db.expire_all()
        problems = _verify(db)
        if problems:
            print()
            print("VERIFICATION FAILED:")
            for problem in problems:
                print(f"  - {problem}")
            return 1

        print()
        _report(db)
        print()
        print("Verified against setup_menu_sections' baseline. That script can now run.")
        return 0

    except Exception:
        db.rollback()
        raise
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
