#!/bin/sh
echo "Creating tables..."
python -c "from app.db import engine, Base; Base.metadata.create_all(bind=engine); print('Tables created')"
echo "Seeding data..."
python -m scripts.seed || echo "Seed skipped (may already exist)"
echo "Starting server on port ${PORT:-8000}..."
exec uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}
