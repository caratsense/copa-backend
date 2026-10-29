#!/bin/sh
echo "Creating tables..."
python -c "from app.db import engine, Base; Base.metadata.create_all(bind=engine); print('Tables created')"

echo "Running migrations..."
python -c "
from app.db.session import engine
from sqlalchemy import text, inspect
conn = engine.connect()
inspector = inspect(engine)
columns = [c['name'] for c in inspector.get_columns('orders')]
if 'payment_method' not in columns:
    conn.execute(text('ALTER TABLE orders ADD COLUMN payment_method VARCHAR DEFAULT \'ONLINE\''))
    print('Added payment_method column')
if 'payment_id' not in columns:
    conn.execute(text('ALTER TABLE orders ADD COLUMN payment_id VARCHAR'))
    print('Added payment_id column')
if 'date_of_birth' not in [c['name'] for c in inspector.get_columns('users')]:
    conn.execute(text('ALTER TABLE users ADD COLUMN date_of_birth DATE'))
    print('Added date_of_birth column')
if 'fulfilment' not in columns:
    # Existing rows become DELIVERY, which is what every one of them was
    # treated as. The two self-pickup orders already in the table are left
    # alone deliberately - they are closed, and rewriting history to say
    # something the shop never acted on would be worse than a wrong label.
    conn.execute(text(\"ALTER TABLE orders ADD COLUMN fulfilment VARCHAR NOT NULL DEFAULT 'DELIVERY'\"))
    conn.execute(text('CREATE INDEX IF NOT EXISTS ix_orders_fulfilment ON orders (fulfilment)'))
    print('Added fulfilment column')
if 'proof_image' not in columns:
    conn.execute(text('ALTER TABLE orders ADD COLUMN proof_image VARCHAR'))
    conn.execute(text('ALTER TABLE orders ADD COLUMN proof_verified_at TIMESTAMPTZ'))
    conn.execute(text('ALTER TABLE orders ADD COLUMN proof_verified_by INTEGER'))
    conn.execute(text('ALTER TABLE orders ADD COLUMN handover_image VARCHAR'))
    print('Added proof columns')
conn.commit()
conn.close()
print('Migrations done')
"

echo "Adding any new order statuses..."
python -c "
from app.db.session import engine
from sqlalchemy import text
# status is a native Postgres enum, so a new member is a migration rather than
# a line of Python - and ADD VALUE wants its own transaction, hence AUTOCOMMIT.
# IF NOT EXISTS makes a redeploy a no-op.
labels = ('READY_FOR_PICKUP', 'COLLECTED', 'NOT_COLLECTED')
with engine.connect().execution_options(isolation_level='AUTOCOMMIT') as conn:
    for label in labels:
        conn.execute(text(\"ALTER TYPE orderstatus ADD VALUE IF NOT EXISTS '\" + label + \"'\"))
print('Order statuses up to date')
"

echo "Checking if seed needed..."
python -c "
from app.db.session import SessionLocal
from app.models.user import User
db = SessionLocal()
count = db.query(User).count()
db.close()
if count == 0:
    print('Database empty — seeding...')
    exit(0)
else:
    print(f'Database has {count} users — skipping seed')
    exit(1)
" && python -m scripts.seed || echo "Seed skipped"
echo "Starting server on port ${PORT:-8000}..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
