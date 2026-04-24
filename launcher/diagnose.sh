#!/usr/bin/env bash
# ── CashGuard Diagnostics ─────────────────────────────────────────────────────
# Run this from the project root to check every component
# Usage: bash launcher/diagnose.sh
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; RESET='\033[0m'
ok()   { echo -e "  ${GREEN}✓${RESET}  $*"; }
fail() { echo -e "  ${RED}✗${RESET}  $*"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $*"; }
info() { echo -e "  ${CYAN}▸${RESET}  $*"; }

PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$PROJECT_ROOT/venv"
API_URL="http://localhost:8000"
WEB_URL="http://localhost:3000"

echo ""
echo "════════════════════════════════════════════════════════════"
echo "  CashGuard Diagnostics"
echo "  Project: $PROJECT_ROOT"
echo "  Time:    $(date)"
echo "════════════════════════════════════════════════════════════"
echo ""

# ── 1. .env file ─────────────────────────────────────────────────────────────
echo "1. Environment (.env)"
ENV_FILE="$PROJECT_ROOT/.env"
if [ -f "$ENV_FILE" ]; then
    ok ".env found"
    SECRET_KEY=$(grep "^SECRET_KEY=" "$ENV_FILE" | cut -d= -f2 || echo "")
    if [ -n "$SECRET_KEY" ] && [ "$SECRET_KEY" != "your-secret-key" ]; then
        ok "SECRET_KEY set (${SECRET_KEY:0:8}...)"
    else
        fail "SECRET_KEY missing or placeholder — login will fail on every restart!"
    fi
    ADMIN_EMAIL=$(grep "^ADMIN_EMAIL=" "$ENV_FILE" | cut -d= -f2 || echo "")
    ADMIN_PASS=$(grep "^ADMIN_PASSWORD=" "$ENV_FILE" | cut -d= -f2 || echo "")
    ok "ADMIN_EMAIL: $ADMIN_EMAIL"
    ok "ADMIN_PASSWORD: ${ADMIN_PASS:0:4}****"
else
    fail ".env file NOT FOUND at $ENV_FILE"
fi
echo ""

# ── 2. Python venv ────────────────────────────────────────────────────────────
echo "2. Python Virtual Environment"
if [ -f "$VENV/bin/python" ]; then
    PYTHON="$VENV/bin/python"
    PY_VER=$("$PYTHON" --version 2>&1)
    ok "venv Python: $PY_VER"
    if "$VENV/bin/pip" show fastapi &>/dev/null; then
        FA_VER=$("$VENV/bin/pip" show fastapi | grep ^Version | awk '{print $2}')
        ok "FastAPI: $FA_VER"
    else
        fail "FastAPI not installed in venv"
    fi
else
    fail "Python venv not found at $VENV"
    PYTHON=python3
fi
echo ""

# ── 3. Config loading ─────────────────────────────────────────────────────────
echo "3. Config / Settings"
if [ -f "$VENV/bin/python" ]; then
    source "$ENV_FILE" 2>/dev/null || true
    CONFIG_CHECK=$("$PYTHON" -c "
import sys; sys.path.insert(0,'$PROJECT_ROOT/apps/api')
from app.core.config import settings, _ENV_FILE
print('env_file:', _ENV_FILE)
print('exists:', _ENV_FILE.exists())
print('secret_key_len:', len(settings.SECRET_KEY))
print('secret_prefix:', settings.SECRET_KEY[:8])
print('admin_email:', settings.ADMIN_EMAIL)
print('app_mode:', settings.APP_MODE)
" 2>&1)
    echo "$CONFIG_CHECK" | while IFS= read -r line; do info "$line"; done
    if echo "$CONFIG_CHECK" | grep -q "exists: True"; then
        ok ".env loaded by pydantic-settings"
    else
        fail ".env NOT loaded by pydantic-settings — SECRET_KEY will be random!"
    fi
fi
echo ""

# ── 4. Database connectivity ───────────────────────────────────────────────────
echo "4. Database"
if pg_isready -h localhost -p 5432 -U cashguard 2>/dev/null; then
    ok "PostgreSQL is running and accepting connections"
else
    fail "PostgreSQL not accessible on localhost:5432"
    warn "Tip: Start Docker first — the launcher starts Docker automatically"
fi
echo ""

# ── 5. API server ─────────────────────────────────────────────────────────────
echo "5. API Server (port 8000)"
API_RUNNING=false
if lsof -ti:8000 &>/dev/null; then
    API_PID=$(lsof -ti:8000 | head -1)
    ok "Something is listening on port 8000 (PID: $API_PID)"
    # Test the health endpoint
    HEALTH=$(curl -s --max-time 3 "$API_URL/v1/health/live" 2>/dev/null || echo "connection_refused")
    if echo "$HEALTH" | grep -q '"status"'; then
        ok "API health endpoint responded"
        API_RUNNING=true
    else
        fail "API not responding: $HEALTH"
    fi
else
    warn "API not running on port 8000 — not started yet"
fi
echo ""

# ── 6. Auth test ───────────────────────────────────────────────────────────────
echo "6. Auth Flow"
if $API_RUNNING; then
    ADMIN_EMAIL_VAL=$(grep "^ADMIN_EMAIL=" "$ENV_FILE" | cut -d= -f2)
    ADMIN_PASS_VAL=$(grep "^ADMIN_PASSWORD=" "$ENV_FILE"  | cut -d= -f2)
    info "Testing login with: $ADMIN_EMAIL_VAL"
    LOGIN_RESP=$(curl -s --max-time 5 -X POST "$API_URL/v1/auth/login" \
        -H "Content-Type: application/json" \
        -d "{\"email\":\"$ADMIN_EMAIL_VAL\",\"password\":\"$ADMIN_PASS_VAL\"}" 2>/dev/null)
    if echo "$LOGIN_RESP" | grep -q '"access_token"'; then
        TOKEN=$(echo "$LOGIN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['access_token'])" 2>/dev/null)
        ok "Login succeeded — token: ${TOKEN:0:20}..."
        # Test /auth/me
        ME_RESP=$(curl -s --max-time 5 "$API_URL/v1/auth/me" \
            -H "Authorization: Bearer $TOKEN" 2>/dev/null)
        if echo "$ME_RESP" | grep -q '"email"'; then
            ME_EMAIL=$(echo "$ME_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['email'])" 2>/dev/null)
            ok "/auth/me works — logged in as: $ME_EMAIL"
        else
            fail "/auth/me failed: $ME_RESP"
        fi
    elif echo "$LOGIN_RESP" | grep -q '"detail"'; then
        DETAIL=$(echo "$LOGIN_RESP" | python3 -c "import sys,json; print(json.load(sys.stdin)['detail'])" 2>/dev/null)
        fail "Login failed: $DETAIL"
        warn "Possible causes:"
        warn "  - Admin user not seeded (run: python -m alembic upgrade head)"
        warn "  - Wrong password in .env"
        warn "  - SECRET_KEY mismatch between restarts"
    else
        fail "Unexpected login response: $LOGIN_RESP"
    fi
else
    warn "Skipping auth test — API not running"
fi
echo ""

# ── 7. Frontend ────────────────────────────────────────────────────────────────
echo "7. Frontend (port 3000)"
if lsof -ti:3000 &>/dev/null; then
    ok "Frontend is running on port 3000"
    FRONT=$(curl -s --max-time 3 -o /dev/null -w "%{http_code}" "$WEB_URL" 2>/dev/null)
    ok "HTTP status: $FRONT"
else
    warn "Frontend not running on port 3000 — not started yet"
fi
echo ""

# ── 8. Log tail ───────────────────────────────────────────────────────────────
echo "8. Recent API Logs"
LOG="$PROJECT_ROOT/logs/api.log"
if [ -f "$LOG" ]; then
    ok "Log found: $LOG"
    echo ""
    echo "  Last 30 lines:"
    tail -30 "$LOG" | while IFS= read -r line; do
        if echo "$line" | grep -qi "error\|exception\|traceback"; then
            echo -e "  ${RED}$line${RESET}"
        else
            echo "  $line"
        fi
    done
else
    warn "No API log found at $LOG — API has not been started yet"
fi
echo ""

echo "════════════════════════════════════════════════════════════"
echo "  Diagnostics complete."
echo "  → If API is not running: double-click '2. Start CashGuard.command'"
echo "  → If login fails: paste the output above into the chat"
echo "════════════════════════════════════════════════════════════"
echo ""
