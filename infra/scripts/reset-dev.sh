#!/usr/bin/env bash
# Reset development environment: wipe DB, re-migrate, re-seed.
set -euo pipefail

echo "⚠️  This will destroy ALL local data."
read -rp "Type 'yes' to confirm: " confirm
[ "$confirm" = "yes" ] || { echo "Aborted."; exit 0; }

cd "$(dirname "$0")/../../apps/api"

echo "→ Dropping all tables..."
alembic downgrade base

echo "→ Recreating schema..."
alembic upgrade head

echo "→ Seeding data..."
python -m app.db.seed

echo "✅ Dev environment reset complete."
