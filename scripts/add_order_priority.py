"""
Add orders.priority to the database.

WHY THIS IS A SCRIPT AND NOT AUTOMATIC
--------------------------------------
`Base.metadata.create_all` at startup adds missing *tables*. It does not add a
column to a table that already exists, and `orders` exists everywhere. So the
column has to be added deliberately. Same reasoning as
scripts/add_pricing_unit.py, and the same shape.

WHAT THE COLUMN IS FOR
----------------------
How urgent an order is, set by hand from the order page.

  NULL    — an ordinary order. Every order that exists today.
  'RUSH'  — do this one first.

It is not a status. Nothing gates on it, no transition reads it and no
WhatsApp message mentions it: it is the shop ranking its own work, so the
kitchen queue can put the urgent one at the top. Nullable with no default,
which is what makes this inert for existing data — an order with nothing set
is exactly the order it was before the column existed.

It is deliberately absent from OrderTrackingRead. How the shop ranks its own
work is not the customer's business, and "RUSH" on their tracking page would
read as a promise nobody made to them.

WHAT IT DOES
------------
1. Reports whether orders.priority already exists, and what the rows hold.
2. With --apply, runs:
       ALTER TABLE orders ADD COLUMN IF NOT EXISTS priority VARCHAR(16)
   No default and no NOT NULL, so PostgreSQL adds it in metadata alone: no
   table rewrite, no row touched, no lock worth the name even on a busy table.
3. Re-reads on a fresh connection and verifies every existing row is NULL.

WHAT IT WILL NOT DO
-------------------
- It never drops, renames or rewrites anything.
- It never touches any column other than the one it adds.
- It never creates, edits or deletes an order row.
- It is idempotent. Run it twice and the second run reports "already present".

USAGE
-----
    # look, change nothing (default)
    python -m scripts.add_order_priority

    # actually apply it
    python -m scripts.add_order_priority --apply

DATABASE_URL is read from the environment. On Railway this must run *inside*
the container: `railway run` executes locally with the env injected, and the
private hostname `postgres.railway.internal` does not resolve from a
workstation. Use `railway ssh`, which runs inside the deployed container:

    railway ssh --service copa-backend "python -m scripts.add_order_priority"
    railway ssh --service copa-backend "python -m scripts.add_order_priority --apply"

ORDER OF OPERATIONS
-------------------
Apply the column BEFORE deploying the code that reads it. The model declares
it, so every order query — the admin queue, the order page, the baker's
screen, checkout — 500s until the column lands. Applying it early is safe: an
unused nullable column is inert and nothing reads it until the code ships.

ROLLING BACK
------------
    ALTER TABLE orders DROP COLUMN priority;

Only after reverting the code, since the model declares the column. Reverting
the deploy and leaving the column in place is also a complete rollback in
practice — nothing else reads it.
"""

import os
import sys

from sqlalchemy import create_engine, text

TABLE = "orders"
COLUMN = "priority"


def _column(conn):
    """The column's definition, or None when it does not exist yet."""
    return conn.execute(text("""
        SELECT data_type, is_nullable, column_default
        FROM information_schema.columns
        WHERE table_name = :t AND column_name = :c
    """), {"t": TABLE, "c": COLUMN}).first()


def _report_rows(conn):
    """Print how many orders hold each value. Returns {value: count}."""
    counts = dict(conn.execute(text(f"""
        SELECT {COLUMN}, count(*) FROM {TABLE} GROUP BY {COLUMN} ORDER BY 1
    """)).all())
    total = sum(counts.values())
    print(f"  {total} order row(s):")
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
            _report_rows(conn)
            print()
            print("Nothing to do.")
            return 0

        print(f"{TABLE}.{COLUMN} does not exist yet.")
        order_count = conn.execute(text(f"SELECT count(*) FROM {TABLE}")).scalar()
        print(f"  {order_count} existing order row(s), all of which stay NULL.")
        print()

        if not apply_it:
            print("WOULD RUN (nothing has been changed):")
            print(f"  ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLUMN} VARCHAR(16);")
            print()
            print("This is additive and nullable with no default, so PostgreSQL")
            print("changes catalogue metadata only: no row is rewritten, no")
            print("existing column is read or written, and no order changes.")
            print()
            print("Re-run with --apply to perform it.")
            return 0

    with engine.begin() as conn:
        conn.execute(text(
            f"ALTER TABLE {TABLE} ADD COLUMN IF NOT EXISTS {COLUMN} VARCHAR(16)"
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
        set_rows = {v: c for v, c in counts.items() if v is not None}
        if set_rows:
            print()
            print(f"VERIFICATION FAILED: expected every existing row to be NULL, "
                  f"found {set_rows}")
            return 1

    print()
    print("Every existing order is unflagged, which is the behaviour they")
    print("already had. Nothing reads this column until the code ships.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
