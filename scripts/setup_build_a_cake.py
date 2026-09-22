"""
Prepare the data the Build-a-Cake flow needs. Inspect by default; --apply writes.

TWO THINGS, both data rather than behaviour:

1. flavor_rules.sponge
   Which sponge each flavour is made on. The column is added here because
   `create_all` does not alter an existing table, and the values are set from
   the groups the seed file already documents - not guessed from the names.
   Guessing would be wrong: "White Chocolate Raspberry Pistachio" is a
   VANILLA-sponge flavour, and so is "Vanilla Orange Chocolate Crumble". A
   name rule gets both wrong and offers the customer a cake the kitchen does
   not make.

2. size_rules
   Build-a-Cake sells up to 3.5kg. Sizes above that are a conversation, not a
   checkout, so 5kg is DEACTIVATED rather than deleted - deleting it would
   break the pricing of any order that used it, and deactivating is what the
   pricing engine already understands ("Unknown size" for anything inactive).
   2.5kg and 3.5kg are added because the range should not jump 2 -> 3 -> 5.

Existing orders are unaffected either way: an order stores its size as text in
the line's customization, and the rules are only consulted when pricing a new
one.

    python -m scripts.setup_build_a_cake            # inspect
    python -m scripts.setup_build_a_cake --apply    # write

On Railway:
    railway ssh --service copa-backend "python -m scripts.setup_build_a_cake --apply"
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect as sa_inspect, text

from app.db import SessionLocal, engine
from app.models.pricing import FlavorRule, SizeRule

VANILLA, CHOCOLATE = "vanilla", "chocolate"

# Taken from the groupings in scripts/seed.py, which is where the client's
# flavour list was transcribed. Any flavour not named here keeps sponge=NULL
# and simply does not appear in the sponge-first flow.
FLAVOUR_SPONGE = {
    # Vanilla based
    "Plain Vanilla White Chocolate": VANILLA,
    "Vanilla Pineapple": VANILLA,
    "Vanilla Mix Fruit": VANILLA,
    "Vanilla Cookie Cream": VANILLA,
    "Vanilla Strawberry": VANILLA,
    "Vanilla Mango": VANILLA,
    # Vanilla based, premium
    "Vanilla Orange Chocolate Crumble": VANILLA,
    "White Chocolate Raspberry Pistachio": VANILLA,
    "White Chocolate Rose Pistachio": VANILLA,
    "White Chocolate Apple Cinnamon": VANILLA,
    "Vanilla Mango Crumble": VANILLA,
    "Vanilla Strawberry Crumble": VANILLA,
    "Vanilla Pineapple Chocolate": VANILLA,
    "Vanilla Blueberry Crumble": VANILLA,
    "Vanilla Blueberry Lemon Curd": VANILLA,
    # Chocolate based
    "Plain Chocolate": CHOCOLATE,
    "Chocolate Coffee": CHOCOLATE,
    "Chocolate Hazelnut": CHOCOLATE,
    "Chocolate Irish Cream": CHOCOLATE,
    "Chocolate Orange": CHOCOLATE,
    "Chocolate Strawberry": CHOCOLATE,
    "Chocolate Mango": CHOCOLATE,
    # Chocolate premium, Belgian
    "Plain Belgian Chocolate": CHOCOLATE,
    "Belgian Chocolate Irish Cream": CHOCOLATE,
    "Belgian Chocolate Cookie Cream": CHOCOLATE,
    "Belgian Chocolate Orange": CHOCOLATE,
    "Belgian Chocolate Hazelnut": CHOCOLATE,
    "Belgian Chocolate Pineapple": CHOCOLATE,
    "Belgian Chocolate Butterscotch": CHOCOLATE,
    "Belgian Chocolate Strawberry": CHOCOLATE,
    "Belgian Chocolate Mango": CHOCOLATE,
    "Belgian Chocolate Mango Crumble": CHOCOLATE,
    "Belgian Chocolate Coffee Salted Caramel": CHOCOLATE,
    "Belgian Chocolate Orange Hazelnut Crumble": CHOCOLATE,
}

# The ladder Build-a-Cake offers. Multipliers stay proportional to the per-kg
# base price, which is what SizeRule has always meant.
WANTED_SIZES = {"500g": 0.5, "1kg": 1.0, "1.5kg": 1.5, "2kg": 2.0,
                "2.5kg": 2.5, "3kg": 3.0, "3.5kg": 3.5}

# Anything heavier is a Contact Us conversation, so it must not be orderable.
MAX_ORDERABLE_KG = 3.5


def _column_exists(table: str, column: str) -> bool:
    return column in {c["name"] for c in sa_inspect(engine).get_columns(table)}


def _kg(name: str, multiplier: float) -> float:
    """A size's weight in kg. The multiplier is per-kg, so it IS the weight."""
    return float(multiplier)


def main() -> int:
    apply_it = "--apply" in sys.argv
    problems = []

    print("flavor_rules.sponge")
    has_column = _column_exists("flavor_rules", "sponge")
    print(f"  column present: {has_column}")
    if not has_column:
        stmt = "ALTER TABLE flavor_rules ADD COLUMN IF NOT EXISTS sponge VARCHAR"
        if not apply_it:
            print(f"  WOULD RUN: {stmt}")
        else:
            with engine.begin() as conn:
                conn.execute(text(stmt))
                conn.execute(text(
                    "CREATE INDEX IF NOT EXISTS ix_flavor_rules_sponge "
                    "ON flavor_rules (sponge)"
                ))
            print(f"  added the column")

    db = SessionLocal()
    try:
        if _column_exists("flavor_rules", "sponge"):
            rows = {f.name: f for f in db.query(FlavorRule).all()}
            unknown = [n for n in FLAVOUR_SPONGE if n not in rows]
            if unknown:
                problems.append(f"flavours in the mapping with no row: {unknown}")
            unmapped = [n for n in rows if n not in FLAVOUR_SPONGE]

            changes = 0
            for name, sponge in FLAVOUR_SPONGE.items():
                row = rows.get(name)
                if row is None or row.sponge == sponge:
                    continue
                print(f"  {'SET   ' if apply_it else 'would set'} {name!r} -> {sponge}")
                if apply_it:
                    row.sponge = sponge
                changes += 1
            print(f"  {changes} flavour(s) to label; "
                  f"{len(unmapped)} left unlabelled (not in the sponge flow)")
            if unmapped:
                for n in unmapped:
                    print(f"      unlabelled: {n}")

        print()
        print("size_rules")
        sizes = {s.name: s for s in db.query(SizeRule).all()}
        for name, multiplier in WANTED_SIZES.items():
            row = sizes.get(name)
            if row is None:
                print(f"  {'ADD   ' if apply_it else 'would add'} {name} (x{multiplier})")
                if apply_it:
                    db.add(SizeRule(name=name, multiplier=multiplier, is_active=True))
            elif not row.is_active:
                print(f"  {'ENABLE' if apply_it else 'would enable'} {name}")
                if apply_it:
                    row.is_active = True

        for name, row in sizes.items():
            if name in WANTED_SIZES or not row.is_active:
                continue
            if _kg(name, row.multiplier) > MAX_ORDERABLE_KG:
                print(f"  {'PAUSE ' if apply_it else 'would pause'} {name} "
                      f"(x{row.multiplier}) - above {MAX_ORDERABLE_KG}kg is Contact Us")
                if apply_it:
                    row.is_active = False
            else:
                print(f"  leaving {name} (x{row.multiplier}) active - not a "
                      f"Build-a-Cake size but under the limit")

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
        by_sponge = {}
        for f in db.query(FlavorRule).filter(FlavorRule.is_active == True).all():
            by_sponge.setdefault(f.sponge, []).append(f.name)
        for sponge in (VANILLA, CHOCOLATE, None):
            print(f"  {str(sponge):<10}: {len(by_sponge.get(sponge, []))} flavour(s)")
        active = [(s.name, s.multiplier) for s in
                  db.query(SizeRule).filter(SizeRule.is_active == True)
                  .order_by(SizeRule.multiplier).all()]
        print(f"  orderable sizes: {[n for n, _ in active]}")
        over = [n for n, m in active if _kg(n, m) > MAX_ORDERABLE_KG]
        if over:
            print(f"  VERIFICATION FAILED: still orderable above "
                  f"{MAX_ORDERABLE_KG}kg: {over}")
            return 1
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())
