"""
Add the DELIVERY_FAILED order status to the live database.

WHY THIS IS A SCRIPT AND NOT AUTOMATIC
--------------------------------------
`Base.metadata.create_all` at startup adds missing *tables*. It does not alter
existing ones, and it cannot add a value to a native PostgreSQL enum type. If
`orders.status` is backed by a native enum, the new value has to be added with
`ALTER TYPE ... ADD VALUE`, and that is a deliberate, reviewable step rather
than something a deploy should do behind your back.

WHAT IT DOES
------------
1. Reports what the column actually is. If `orders.status` is a VARCHAR, there
   is nothing to do at all and the script says so and exits.
2. If it is a native enum, adds DELIVERY_FAILED to it.

WHAT IT WILL NOT DO
-------------------
- It never drops, renames or rewrites anything.
- It never touches a row of order data.
- Adding a value to an enum does not rewrite the table and cannot fail on
  existing rows: every current value stays valid.
- It is idempotent. Run it twice and the second run reports "already present".

USAGE
-----
    # look, change nothing (default)
    python -m scripts.add_delivery_failed_status

    # actually apply it
    python -m scripts.add_delivery_failed_status --apply

DATABASE_URL is read from the environment. On Railway this must run *inside*
the container: `railway run` executes locally with the env injected, and the
private hostname `postgres.railway.internal` does not resolve from a
workstation. Use `railway ssh`, which runs inside the deployed container:

    railway ssh --service copa-backend "python -m scripts.add_delivery_failed_status"
    railway ssh --service copa-backend "python -m scripts.add_delivery_failed_status --apply"

ROLLING BACK
------------
PostgreSQL cannot remove a value from an enum. If you need to undo this, the
value simply stays in the type unused — it is inert. Nothing reads it unless
the application code creates it, so reverting the deploy is a complete rollback
in practice.
"""

import os
import sys

from sqlalchemy import create_engine, text

NEW_VALUE = "DELIVERY_FAILED"
TABLE = "orders"
COLUMN = "status"


def main() -> int:
    apply_it = "--apply" in sys.argv

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        print("DATABASE_URL is not set.")
        return 2
    if url.startswith("postgres://"):
        url = url.replace("postgres://", "postgresql://", 1)

    if not url.startswith("postgresql"):
        print(f"Not a PostgreSQL database ({url.split(':')[0]}). Nothing to do —")
        print("only PostgreSQL has native enum types that need altering.")
        return 0

    engine = create_engine(url)

    with engine.connect() as conn:
        row = conn.execute(text("""
            SELECT data_type, udt_name
            FROM information_schema.columns
            WHERE table_name = :t AND column_name = :c
        """), {"t": TABLE, "c": COLUMN}).first()

        if row is None:
            print(f"Column {TABLE}.{COLUMN} does not exist. Is DATABASE_URL "
                  f"pointing at the right database?")
            return 2

        print(f"{TABLE}.{COLUMN}: data_type={row.data_type}  udt_name={row.udt_name}")

        # A plain text column accepts the new value with no migration at all.
        if row.data_type != "USER-DEFINED":
            print()
            print("This column is not a native enum, so it already accepts the new")
            print("value. NOTHING TO DO — deploy the code and you are finished.")
            return 0

        enum_type = row.udt_name
        values = conn.execute(text("""
            SELECT enumlabel
            FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
            WHERE t.typname = :n
            ORDER BY e.enumsortorder
        """), {"n": enum_type}).scalars().all()

        print(f"enum {enum_type} currently has {len(values)} values:")
        print("  " + ", ".join(values))
        print()

        if NEW_VALUE in values:
            print(f"{NEW_VALUE} is already present. Nothing to do.")
            return 0

        if not apply_it:
            print("WOULD RUN (nothing has been changed):")
            print(f'  ALTER TYPE {enum_type} ADD VALUE IF NOT EXISTS \'{NEW_VALUE}\';')
            print()
            print("This is additive: no table rewrite, no row is read or written,")
            print("and every existing value stays valid.")
            print()
            print("Re-run with --apply to perform it.")
            return 0

    # ALTER TYPE ... ADD VALUE has to be durable the moment it succeeds.
    # Emitting a bare COMMIT and then running the ALTER does not achieve
    # that: psycopg2 opens a fresh transaction for the next statement, and
    # SQLAlchemy rolls that back when the connection closes, discarding the
    # change. AUTOCOMMIT leaves no transaction to roll back.
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as conn:
        conn.execute(text(
            f'ALTER TYPE "{enum_type}" ADD VALUE IF NOT EXISTS \'{NEW_VALUE}\''
        ))
    print(f"Added {NEW_VALUE} to {enum_type}.")

    # Verify on a fresh connection so we are reading committed state.
    with engine.connect() as conn:
        values = conn.execute(text("""
            SELECT enumlabel
            FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid
            WHERE t.typname = :n
            ORDER BY e.enumsortorder
        """), {"n": enum_type}).scalars().all()

    if NEW_VALUE in values:
        print(f"Verified: {enum_type} now has {len(values)} values.")
        return 0

    print(f"FAILED: {NEW_VALUE} is still not present. Do not deploy the code.")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
