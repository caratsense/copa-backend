"""
Add products.pricing_unit to the database.

WHY THIS IS A SCRIPT AND NOT AUTOMATIC
--------------------------------------
`Base.metadata.create_all` at startup adds missing *tables*. It does not add a
column to a table that already exists, and `products` exists everywhere. So the
column has to be added deliberately.

It is deliberately NOT in the boot-time ALTER list in app/main.py. That list
swallows failures with a print and carries on, so a column that failed to
appear leaves the database half-migrated with no signal — and it only runs when
the FastAPI app boots, which means any script that touches `products` before
the app has started (scripts.seed, scripts.setup_menu_sections) would hit a
column the model expects and the table does not have. Running this explicitly
removes the ordering question: apply the column, confirm, then deploy the code.

WHAT THE COLUMN IS FOR
----------------------
Whether a product's size selection multiplies its price.

  'kg'    — base_price is a per-kg price; SizeRule.multiplier applies.
            This is what every existing product already is.
  'fixed' — base_price is the price of the thing; no size multiplier.
            A brownie, a loaf, a pack of six buns.

Defaulting to 'kg' is what makes this a no-op for existing data: the backfill
writes down the assumption the pricing engine was already making, rather than
changing it. No product's price moves.

WHAT IT DOES
------------
1. Reports whether products.pricing_unit already exists, and what the current
   rows hold.
2. With --apply, runs:
       ALTER TABLE products ADD COLUMN IF NOT EXISTS pricing_unit
           VARCHAR NOT NULL DEFAULT 'kg'
   PostgreSQL backfills every existing row to 'kg' in that one statement.
3. Re-reads on a fresh connection and verifies every product resolves to 'kg'.

WHAT IT WILL NOT DO
-------------------
- It never drops, renames or rewrites anything.
- It never touches base_price, or any column other than the one it adds.
- It never creates, edits or deletes a product row.
- It is idempotent. Run it twice and the second run reports "already present".

USAGE
-----
    # look, change nothing (default)
    python -m scripts.add_pricing_unit

    # actually apply it
    python -m scripts.add_pricing_unit --apply

DATABASE_URL is read from the environment. On Railway this must run *inside*
the container: `railway run` executes locally with the env injected, and the
private hostname `postgres.railway.internal` does not resolve from a
workstation. Use `railway ssh`, which runs inside the deployed container:

    railway ssh --service copa-backend "python -m scripts.add_pricing_unit"
    railway ssh --service copa-backend "python -m scripts.add_pricing_unit --apply"

ORDER OF OPERATIONS
-------------------
Apply the column BEFORE deploying the code that reads it. The reverse order
500s every product query — the menu, /products and checkout — until the column
lands. Applying it early is safe: an unused column with a default is inert and
nothing reads it until the model declares it.

ROLLING BACK
------------
Unlike an enum value, this is cleanly reversible:

    ALTER TABLE products DROP COLUMN pricing_unit;

Do that only after reverting the code, since the model requires the column.
Simply reverting the deploy and leaving the column in place is also a complete
rollback in practice — nothing reads it.
"""

import os
import sys

from sqlalchemy import create_engine, text

TABLE = "products"
COLUMN = "pricing_unit"
DEFAULT = "kg"


def _column(conn):
    """The column's definition, or None when it does not exist yet."""
    return conn.execute(text("""
        SELECT data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = :t AND column_name = :c
    """), {"t": TABLE, "c": COLUMN}).first()


def _report_rows(conn):
    """Print how many products hold each value. Returns {value: count}."""
    counts = dict(conn.execute(text(f"""
        SELECT {COLUMN}, count(*) FROM {TABLE} GROUP BY {COLUMN} ORDER BY 1
    """)).all())
    total = sum(counts.values())
    print(f"  {total} product row(s):")
    for value, count in counts.items():
        print(f"    {value!r}: {count}")
    return counts


def main() -> int:
    apply_it = "--apply" in sys.argv

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set.")
        return 2
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    if not url.startswith("postgresql"):
        print(f"Not a PostgreSQL database ({url.split(':')[0]}).")
        print("This script targets PostgreSQL. Nothing has been changed.")
        return 2

    engine = create_engine(url)

    with engine.connect() as conn:
        if conn.execute(text(
            "SELECT to_regclass(:t)"
        ), {"t": TABLE}).scalar() is None:
            print(f"Table {TABLE} does not exist. Is DATABASE_URL pointing at "
                  f"the right database?")
            return 2

        existing = _column(conn)

        if existing is not None:
            print(f"{TABLE}.{COLUMN} ALREADY EXISTS: "
                  f"data_type={existing.data_type}  "
                  f"nullable={existing.is_nullable}  "
                  f"default={existing.column_default}")
            counts = _report_rows(conn)
            stray = {v: c for v, c in counts.items() if v != DEFAULT}
            print()
            if stray:
                # Not a failure: once fixed-price products exist this is the
                # expected state. Say so plainly rather than implying a problem.
                print(f"Note: {sum(stray.values())} row(s) hold a value other "
                      f"than {DEFAULT!r}: {stray}")
                print("That is expected once fixed-price products have been created.")
            print("Nothing to do.")
            return 0

        print(f"{TABLE}.{COLUMN} does not exist yet.")
        product_count = conn.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar()
        print(f"  {product_count} existing product row(s) would be backfilled to {DEFAULT!r}.")
        print()

        if not apply_it:
            print("WOULD RUN (nothing has been changed):")
            print(f"  ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLUMN} "
                  f"VARCHAR NOT NULL DEFAULT '{DEFAULT}';")
            print()
            print("This is additive. No existing column is read or written, no")
            print(f"price changes, and every existing row becomes {DEFAULT!r} —")
            print("which is the behaviour those products already have.")
            print()
            print("Re-run with --apply to perform it.")
            return 0

    with engine.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLUMN} "
            f"VARCHAR NOT NULL DEFAULT '{DEFAULT}'"
        ))
    print(f"Added {TABLE}.{COLUMN}.")
    print()

    # Verify on a fresh connection so we are reading committed state.
    with engine.connect() as conn:
        added = _column(conn)
        if added is None:
            print("VERIFICATION FAILED: the column is still not present.")
            return 1
        print(f"Verified: data_type={added.data_type}  "
              f"nullable={added.is_nullable}  default={added.column_default}")

        counts = _report_rows(conn)
        wrong = {v: c for v, c in counts.items() if v != DEFAULT}
        if wrong:
            print()
            print(f"VERIFICATION FAILED: expected every existing row to be "
                  f"{DEFAULT!r}, found {wrong}")
            return 1
        nulls = conn.execute(text(
            f"SELECT count(*) FROM {TABLE} WHERE {COLUMN} IS NULL"
        )).scalar()
        if nulls:
            print(f"\nVERIFICATION FAILED: {nulls} row(s) are NULL.")
            return 1

    print()
    print(f"Every existing product resolves to {DEFAULT!r}. Prices are unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
