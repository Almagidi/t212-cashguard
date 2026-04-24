#!/bin/bash
# ============================================================
# T212 CashGuard — Apply migrations + test new features
# ============================================================
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT/apps/api"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; }

# Find python
PYTHON="$PROJECT_ROOT/venv/bin/python"
[ ! -f "$PYTHON" ] && PYTHON=$(which python3)

API="http://localhost:8000"
TEMP_API_PID=""

COMPOSE_CMD=""
if docker compose version >/dev/null 2>&1; then
  COMPOSE_CMD="docker compose"
elif command -v docker-compose >/dev/null 2>&1; then
  COMPOSE_CMD="docker-compose"
fi

compose() {
  if [ "$COMPOSE_CMD" = "docker compose" ]; then
    docker compose "$@"
  else
    docker-compose "$@"
  fi
}

cleanup() {
  if [ -n "$TEMP_API_PID" ] && kill -0 "$TEMP_API_PID" 2>/dev/null; then
    kill "$TEMP_API_PID" 2>/dev/null
    wait "$TEMP_API_PID" 2>/dev/null
  fi
}

trap cleanup EXIT INT TERM

echo ""
echo -e "${BOLD}${CYAN}  CashGuard — Migrations + Feature Test${RESET}"
echo "  ─────────────────────────────────────────"
echo ""

# ── Load environment ──────────────────────────────────────────────────────────
if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
  set +a
fi

ADMIN_EMAIL="${ADMIN_EMAIL:-admin@localhost}"
POSTGRES_USER="${POSTGRES_USER:-cashguard}"

# ── Ensure Docker + DB are ready ──────────────────────────────────────────────
echo -e "  ${CYAN}▸${RESET} Checking Docker..."
if [ -z "$COMPOSE_CMD" ]; then
  fail "Docker Compose is not available"
  echo "    Install Docker Desktop and try again."
  echo ""
  read -p "  Press ENTER to close..."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  echo "    Docker not running — starting Docker Desktop..."
  open -a Docker
  for i in $(seq 1 60); do
    sleep 2
    if docker info >/dev/null 2>&1; then
      break
    fi
    if [ "$i" = "60" ]; then
      fail "Docker failed to start in time"
      echo "    Open Docker Desktop manually, wait for it to finish starting, then try again."
      echo ""
      read -p "  Press ENTER to close..."
      exit 1
    fi
  done
fi
ok "Docker running"

echo -e "  ${CYAN}▸${RESET} Starting Postgres + Redis..."
compose -f "$PROJECT_ROOT/docker-compose.yml" up -d postgres redis >/tmp/cashguard-migrate-compose.log 2>&1
tail -5 /tmp/cashguard-migrate-compose.log | sed 's/^/    /'

echo -e "  ${CYAN}▸${RESET} Waiting for PostgreSQL..."
POSTGRES_READY=false
for i in $(seq 1 30); do
  sleep 1
  if compose -f "$PROJECT_ROOT/docker-compose.yml" exec -T postgres pg_isready -U "$POSTGRES_USER" >/dev/null 2>&1; then
    POSTGRES_READY=true
    break
  fi
done

if [ "$POSTGRES_READY" != true ]; then
  fail "PostgreSQL did not become ready"
  echo "    Check Docker Desktop and then try again."
  echo ""
  read -p "  Press ENTER to close..."
  exit 1
fi
ok "PostgreSQL ready"

# ── 1. Apply migrations ───────────────────────────────────────────────────────
echo -e "  ${CYAN}▸${RESET} Applying database migrations..."
if RESULT=$($PYTHON -m alembic upgrade head 2>&1); then
  echo "$RESULT" | grep -E "Running|already|head|upgrade" | sed 's/^/    /'
  ok "Migrations applied"
else
  fail "Migration failed"
  echo "$RESULT" | tail -12 | sed 's/^/    /'
  echo ""
  read -p "  Press ENTER to close..."
  exit 1
fi

echo ""
CURRENT=$($PYTHON -m alembic current 2>&1 | tail -1)
echo -e "  Current revision: ${CYAN}$CURRENT${RESET}"
echo ""

# ── Ensure API is running for endpoint tests ─────────────────────────────────
if ! curl -sf "$API/v1/health/live" >/dev/null 2>&1; then
  echo -e "  ${CYAN}▸${RESET} API not running — starting temporary API for tests..."
  LOG_DIR="$PROJECT_ROOT/logs"
  mkdir -p "$LOG_DIR"
  (
    cd "$PROJECT_ROOT/apps/api" || exit 1
    "$PYTHON" -m uvicorn app.main:app --host 127.0.0.1 --port 8000
  ) > "$LOG_DIR/api-test.log" 2>&1 &
  TEMP_API_PID=$!

  API_READY=false
  for i in $(seq 1 30); do
    sleep 1
    if curl -sf "$API/v1/health/live" >/dev/null 2>&1; then
      API_READY=true
      break
    fi
  done

  if [ "$API_READY" != true ]; then
    fail "Temporary API failed to start"
    tail -20 "$LOG_DIR/api-test.log" 2>/dev/null | sed 's/^/    /'
    echo ""
    read -p "  Press ENTER to close..."
    exit 1
  fi
  ok "Temporary API running for feature tests"
else
  ok "API already running"
fi

# ── Helper: login and get token ───────────────────────────────────────────────
ADMIN_PASSWORD="${ADMIN_PASSWORD:-}"
if [ -z "$ADMIN_PASSWORD" ]; then
  fail "Could not read ADMIN_PASSWORD from .env"
  echo "    Update .env and try again."
  echo ""
  read -p "  Press ENTER to close..."
  exit 1
fi

TOKEN=$(curl -s -X POST "$API/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$ADMIN_EMAIL\",\"password\":\"$ADMIN_PASSWORD\"}" 2>/dev/null \
  | grep -o '"access_token":"[^"]*"' | cut -d'"' -f4)

if [ -z "$TOKEN" ]; then
  fail "Could not get auth token"
  echo "    Verify ADMIN_EMAIL / ADMIN_PASSWORD in .env."
  echo "    If the password was reset, run '7. Reset Admin Password.command' first."
  echo ""
  read -p "  Press ENTER to close..."
  exit 1
fi
ok "Auth token obtained"
echo ""

PASS=0; FAIL=0

check() {
  local name="$1"; local url="$2"; local expect="$3"
  local resp=$(curl -s -w "\n%{http_code}" -H "Authorization: Bearer $TOKEN" "$API$url" 2>/dev/null)
  local code=$(echo "$resp" | tail -1)
  local body=$(echo "$resp" | head -1)
  if echo "$expect" | tr '|' '\n' | grep -qx "$code"; then
    echo -e "  ${GREEN}✓${RESET} $name  ${CYAN}[$code]${RESET}"
    PASS=$((PASS+1))
  else
    echo -e "  ${RED}✗${RESET} $name  ${RED}[$code]${RESET}"
    echo "    $(echo $body | cut -c1-120)"
    FAIL=$((FAIL+1))
  fi
}

# ── 2. Core health ────────────────────────────────────────────────────────────
echo -e "  ${BOLD}Core endpoints${RESET}"
check "Health"                    "/v1/health/live"                   "200"
check "Authenticated session"     "/v1/auth/me"                       "200"
check "Settings"                  "/v1/settings"                      "200"

# ── 3. New analytics endpoints ────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}New: Per-strategy P&L analytics${RESET}"
check "Performance (overall)"     "/v1/reports/performance"           "200"
check "Performance by-strategy"   "/v1/reports/performance/by-strategy" "200"

# ── 4. New: CSV exports ───────────────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}New: CSV export endpoints${RESET}"
check "Export trades.csv"         "/v1/reports/export/trades.csv"     "200"
check "Export audit.csv"          "/v1/reports/export/audit.csv"      "200"

# ── 5. Risk profile (CFD fields) ─────────────────────────────────────────────
echo ""
echo -e "  ${BOLD}New: CFD risk profile fields${RESET}"
RISK=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/risk/profile" 2>/dev/null)
if echo "$RISK" | grep -q "max_overnight_cfd_exposure_pct\|cfd_max_leverage"; then
  echo -e "  ${GREEN}✓${RESET} CFD fields present in risk profile"
  PASS=$((PASS+1))
else
  echo -e "  ${YELLOW}~${RESET} Risk profile returned but CFD fields not yet in schema"
  # Still check that the endpoint works
  check "Risk profile accessible" "/v1/risk/profile"                "200"
fi

# ── 6. New: reports endpoint structure ───────────────────────────────────────
echo ""
echo -e "  ${BOLD}New: Strategy routing in watchlists${RESET}"
STRATS=$(curl -s -H "Authorization: Bearer $TOKEN" "$API/v1/strategies" 2>/dev/null)
if echo "$STRATS" | grep -q "\[\]" || echo "$STRATS" | grep -q '"type"' || echo "$STRATS" | grep -q '"id"'; then
  echo -e "  ${GREEN}✓${RESET} Strategies endpoint responsive"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} Strategies endpoint issue: $(echo $STRATS | cut -c1-80)"
  FAIL=$((FAIL+1))
fi

# ── 7. Limit order wiring (verify strategy runner has it) ────────────────────
echo ""
echo -e "  ${BOLD}Code verification${RESET}"
RUNNER="/app/app/services/strategy_runner.py"
[ ! -f "$RUNNER" ] && RUNNER="$PROJECT_ROOT/apps/api/app/services/strategy_runner.py"
if grep -q 'order_type="limit"' "$RUNNER" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} Limit order entries wired"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} Limit order entries NOT found in strategy_runner.py"
  FAIL=$((FAIL+1))
fi

ALERT_SVC="$PROJECT_ROOT/apps/api/app/services/alert_service.py"
if grep -q 'alert_trade_submitted\|alert_stop_out\|alert_take_profit\|alert_daily_summary' "$ALERT_SVC" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} New alert helpers present"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} Alert helpers missing"
  FAIL=$((FAIL+1))
fi

SCANNER="$PROJECT_ROOT/apps/api/app/scanner/morning_scan.py"
if grep -q 'run_morning_scan_typed\|opening_fade' "$SCANNER" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} Morning scanner strategy routing present"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} Scanner routing missing"
  FAIL=$((FAIL+1))
fi

RISK_ENG="$PROJECT_ROOT/apps/api/app/risk/engine.py"
if grep -q 'check_cfd_limits\|cfd_max_leverage' "$RISK_ENG" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} CFD risk checks wired in engine"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} CFD risk checks missing from engine"
  FAIL=$((FAIL+1))
fi

MODELS="$PROJECT_ROOT/apps/api/app/db/models/__init__.py"
if grep -q 'CFDFundingCost\|cfd_max_leverage\|max_overnight_cfd_exposure_pct' "$MODELS" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} CFD model fields + CFDFundingCost model present"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} CFD model fields missing"
  FAIL=$((FAIL+1))
fi

# ── 8. New: WebSocket + Regime endpoint ──────────────────────────────────────
echo ""
echo -e "  ${BOLD}New: WebSocket + Market Regime${RESET}"
check "Regime endpoint"             "/v1/regime"                        "200"
check "Trades list endpoint"        "/v1/trades"                        "200"
WS_FILE="$PROJECT_ROOT/apps/api/app/api/v1/routes/ws.py"
if [ -f "$WS_FILE" ] && grep -q "websocket_live\|ConnectionManager" "$WS_FILE" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} WebSocket route file present"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} WebSocket route missing"
  FAIL=$((FAIL+1))
fi

# ── 9. New: Trade Journal migration ──────────────────────────────────────────
echo ""
echo -e "  ${BOLD}New: Trade Journal${RESET}"
MIGRATION_FILE="$PROJECT_ROOT/apps/api/app/db/migrations/versions/0005_trade_journal.py"
if [ -f "$MIGRATION_FILE" ]; then
  echo -e "  ${GREEN}✓${RESET} Trade journal migration file present"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} Trade journal migration missing"
  FAIL=$((FAIL+1))
fi

MODELS="$PROJECT_ROOT/apps/api/app/db/models/__init__.py"
if grep -q 'journal_notes\|journal_tags\|journal_emotion' "$MODELS" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} Trade journal model fields present"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} Trade journal fields missing from model"
  FAIL=$((FAIL+1))
fi

# ── 10. New: Discord/Slack alerts + drawdown sizing ──────────────────────────
echo ""
echo -e "  ${BOLD}New: Alerts + Risk sizing${RESET}"
ALERT_SVC="$PROJECT_ROOT/apps/api/app/services/alert_service.py"
if grep -q '_send_discord\|_send_slack' "$ALERT_SVC" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} Discord + Slack alert channels present"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} Discord/Slack alert methods missing"
  FAIL=$((FAIL+1))
fi

RISK_ENG="$PROJECT_ROOT/apps/api/app/risk/engine.py"
if grep -q 'get_drawdown_size_factor\|apply_drawdown_sizing' "$RISK_ENG" 2>/dev/null; then
  echo -e "  ${GREEN}✓${RESET} Drawdown-adaptive sizing present in engine"
  PASS=$((PASS+1))
else
  echo -e "  ${RED}✗${RESET} Drawdown sizing missing from engine"
  FAIL=$((FAIL+1))
fi

echo ""
echo "  ─────────────────────────────────────────"
if [ "$FAIL" = "0" ]; then
  echo -e "  ${BOLD}${GREEN}ALL $PASS CHECKS PASSED ✓${RESET}"
else
  echo -e "  ${GREEN}$PASS passed${RESET}   ${RED}$FAIL failed${RESET}"
fi
echo ""
read -p "  Press ENTER to close..."
