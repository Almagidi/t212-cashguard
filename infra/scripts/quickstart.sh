#!/usr/bin/env bash
# ============================================================
# T212 CashGuard Trader — Quick Start Script
# Handles first-time setup automatically.
# Usage: ./infra/scripts/quickstart.sh
# ============================================================
set -euo pipefail

BOLD='\033[1m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
RESET='\033[0m'

info()    { echo -e "${GREEN}✓${RESET} $*"; }
warn()    { echo -e "${YELLOW}⚠${RESET}  $*"; }
error()   { echo -e "${RED}✗${RESET} $*"; exit 1; }
heading() { echo -e "\n${BOLD}$*${RESET}"; }

# ── Check requirements ────────────────────────────────────────────────────────
heading "Checking requirements..."

command -v docker >/dev/null 2>&1 || error "Docker is required. Install from https://docs.docker.com/get-docker/"
command -v python3 >/dev/null 2>&1 || error "Python 3.11+ is required"
command -v node >/dev/null 2>&1 || error "Node.js 20+ is required"

PYTHON_VERSION=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
NODE_VERSION=$(node --version | cut -c2-)

info "Docker: $(docker --version | cut -d' ' -f3 | tr -d ',')"
info "Python: $PYTHON_VERSION"
info "Node: $NODE_VERSION"

# ── Navigate to repo root ─────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"
info "Working directory: $REPO_ROOT"

# ── .env setup ────────────────────────────────────────────────────────────────
heading "Configuring environment..."

if [ ! -f .env ]; then
    cp .env.example .env
    info "Created .env from .env.example"

    # Auto-generate secure keys
    if command -v openssl >/dev/null 2>&1; then
        SECRET_KEY=$(openssl rand -hex 32)
        MASTER_KEY=$(openssl rand -hex 32)
        sed -i.bak "s/change-me-generate-with-openssl-rand-hex-32/$SECRET_KEY/" .env 2>/dev/null || \
            sed -i "" "s/change-me-generate-with-openssl-rand-hex-32/$SECRET_KEY/" .env  # macOS
        # Replace second occurrence for MASTER_KEY
        python3 -c "
content = open('.env').read()
parts = content.split('$SECRET_KEY')
if len(parts) > 1:
    pass  # already replaced, now fix MASTER_KEY
import re
content2 = open('.env').read()
# Replace first remaining placeholder
content2 = content2.replace('change-me-generate-with-openssl-rand-hex-32', '$MASTER_KEY', 1)
open('.env', 'w').write(content2)
"
        info "Generated secure SECRET_KEY and MASTER_KEY"
    else
        warn "openssl not found — please manually set SECRET_KEY and MASTER_KEY in .env"
    fi
else
    info ".env already exists"
fi

# Check if keys have been set
if grep -q "change-me-generate" .env; then
    warn "SECRET_KEY or MASTER_KEY still has default value in .env"
    warn "Run: openssl rand -hex 32  (twice) and update .env"
fi

# ── Start infrastructure ──────────────────────────────────────────────────────
heading "Starting infrastructure (PostgreSQL + Redis)..."

docker compose up -d postgres redis

echo "Waiting for services to be healthy..."
for i in $(seq 1 30); do
    if docker compose exec -T postgres pg_isready -U cashguard >/dev/null 2>&1; then
        break
    fi
    sleep 1
done
info "PostgreSQL ready"

# ── Install backend deps ──────────────────────────────────────────────────────
heading "Installing backend dependencies..."
cd apps/api
pip install -r requirements.txt -q 2>&1 | tail -3
info "Backend dependencies installed"

# ── Run migrations ────────────────────────────────────────────────────────────
heading "Running database migrations..."
DATABASE_URL="postgresql+asyncpg://cashguard:cashguard_secret@localhost:5432/cashguard" \
    alembic upgrade head
info "Migrations complete"

# ── Seed database ─────────────────────────────────────────────────────────────
heading "Seeding database..."
DATABASE_URL="postgresql+asyncpg://cashguard:cashguard_secret@localhost:5432/cashguard" \
    python -m app.db.seed
info "Database seeded"
cd "$REPO_ROOT"

# ── Install frontend deps ─────────────────────────────────────────────────────
heading "Installing frontend dependencies..."
cd apps/web
npm install --silent
info "Frontend dependencies installed"
cd "$REPO_ROOT"

# ── Summary ───────────────────────────────────────────────────────────────────
echo ""
echo -e "${BOLD}════════════════════════════════════════${RESET}"
echo -e "${GREEN}${BOLD}  Setup complete!${RESET}"
echo -e "${BOLD}════════════════════════════════════════${RESET}"
echo ""
echo "  Start dev servers:  make dev"
echo "  Full Docker stack:  make up"
echo "  Run tests:          make test"
echo ""
echo "  Frontend:  http://localhost:3000"
echo "  API docs:  http://localhost:8000/docs"
echo ""
echo "  Login: admin@localhost / (see ADMIN_PASSWORD in .env)"
echo ""

if grep -q "POLYGON_API_KEY=$" .env 2>/dev/null || grep -q "POLYGON_API_KEY=\s*$" .env 2>/dev/null; then
    echo -e "  ${YELLOW}Tip:${RESET} Add POLYGON_API_KEY to .env for real market data"
    echo "       Free key at: https://polygon.io"
    echo ""
fi
