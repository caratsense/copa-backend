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
conn.commit()
conn.close()
print('Migrations done')
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
