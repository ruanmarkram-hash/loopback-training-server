#!/bin/bash
set -e

# Safety net before schema changes: dumps only when migrations are pending
# and a writable verified backup is mandatory before changing existing data.
echo "Pre-migration backup check..."
python -m app.backup pre-migrate

echo "Running database migrations..."
alembic upgrade head

echo "Bootstrapping admin user (if configured)..."
python -m app.cli bootstrap || echo "bootstrap skipped/failed (non-fatal)"

echo "Starting Training API..."
if [ "$ENVIRONMENT" = "LOCAL" ]; then
    exec fastapi dev app/main.py --host 0.0.0.0 --port 8001
else
    exec fastapi run app/main.py --host 0.0.0.0 --port 8001
fi
