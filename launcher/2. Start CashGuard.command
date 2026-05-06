#!/bin/bash
# ============================================================
# T212 CashGuard Trader - Start
# Double-click this file to launch the app.
# ============================================================

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; }
step() { echo -e "  ${CYAN}▸${RESET} $1"; }

API_PID=""
WEB_PID=""
CELERY_PID=""
LOG_DIR="$PROJECT_ROOT/logs"
SHUTDOWN_REQUESTED=0

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

wait_for_url() {
    local url="$1"
    local timeout="${2:-30}"
    local i
    for i in $(seq 1 "$timeout"); do
        if curl -sf "$url" >/dev/null 2>&1; then
            return 0
        fi
        sleep 1
    done
    return 1
}

stop_port_processes() {
    local port="$1"
    local pids

    pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    [ -z "$pids" ] && return 0

    echo "$pids" | while read -r pid; do
        [ -z "$pid" ] && continue
        kill "$pid" 2>/dev/null || true
    done

    sleep 1

    pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    [ -z "$pids" ] && return 0

    echo "$pids" | while read -r pid; do
        [ -z "$pid" ] && continue
        kill -9 "$pid" 2>/dev/null || true
    done
}

cleanup() {
    if [ "$SHUTDOWN_REQUESTED" = "1" ]; then
        return
    fi
    SHUTDOWN_REQUESTED=1

    echo ""
    echo -e "  ${YELLOW}Shutting down...${RESET}"

    for pid in "$API_PID" "$WEB_PID" "$CELERY_PID"; do
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            wait "$pid" 2>/dev/null || true
        fi
    done

    if [ -n "$COMPOSE_CMD" ]; then
        compose -f "$PROJECT_ROOT/docker-compose.yml" stop postgres redis >/dev/null 2>&1 || true
    fi

    rm -f "$PROJECT_ROOT/.pids"

    ok "All services stopped cleanly"
    echo "  (Database data is preserved for next time)"
    echo ""
}

handle_shutdown_signal() {
    cleanup
    exit 0
}

trap cleanup EXIT
trap handle_shutdown_signal INT TERM

clear
echo ""
echo -e "${BOLD}${BLUE}  T212 CashGuard Trader${RESET}   Starting up..."
echo ""

# - Check setup was run -------------------------------------------------------
if [ ! -f "$PROJECT_ROOT/.setup_complete" ]; then
    if [ -f "$PROJECT_ROOT/.env" ] \
        && [ -f "$PROJECT_ROOT/venv/bin/python" ] \
        && [ -d "$PROJECT_ROOT/apps/web/node_modules" ]; then
        warn "Setup marker is missing, but local dependencies are present"
        echo "    Continuing with the existing .env, Python venv, and frontend packages."
        echo "    To recreate the marker later, rerun '1. Setup (Run First).command'."
    else
        fail "Setup marker is missing and local dependencies are incomplete"
        echo ""
        echo "  Run '1. Setup (Run First).command' first."
        echo "  If you already ran setup, check that these exist:"
        echo "    $PROJECT_ROOT/.env"
        echo "    $PROJECT_ROOT/venv/bin/python"
        echo "    $PROJECT_ROOT/apps/web/node_modules"
        echo ""
        read -p "  Press ENTER to close..."
        exit 1
    fi
fi

# - Find Python ---------------------------------------------------------------
VENV_PYTHON="$PROJECT_ROOT/venv/bin/python"

if [ -f "$VENV_PYTHON" ]; then
    PYTHON="$VENV_PYTHON"
    ok "Using virtual environment Python"
else
    warn "No virtual environment found - creating one..."
    PYEXE=""
    for p in /opt/homebrew/bin/python3.12 /opt/homebrew/bin/python3.11 \
             /usr/local/bin/python3.12 /usr/local/bin/python3.11; do
        if [ -f "$p" ]; then
            PYEXE="$p"
            break
        fi
    done
    if [ -z "$PYEXE" ]; then
        fail "Python 3.11+ not found. Please run setup again."
        read -p "  Press ENTER to close..."
        exit 1
    fi
    "$PYEXE" -m venv "$PROJECT_ROOT/venv"
    "$PROJECT_ROOT/venv/bin/python" -m pip install -q -r "$PROJECT_ROOT/apps/api/requirements.txt"
    PYTHON="$VENV_PYTHON"
    ok "Virtual environment created and packages installed"
fi

NPM=$(command -v npm 2>/dev/null || true)
if [ -z "$NPM" ]; then
    fail "npm was not found. Please run setup again."
    read -p "  Press ENTER to close..."
    exit 1
fi

if [ -z "$COMPOSE_CMD" ]; then
    fail "Docker Compose is not available"
    echo "  Install Docker Desktop and try again."
    read -p "  Press ENTER to close..."
    exit 1
fi

# - Load environment ----------------------------------------------------------
if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$PROJECT_ROOT/.env"
    set +a
    ok "Environment loaded from .env"
else
    warn "No .env found - using defaults (mock mode)"
fi

POSTGRES_USER="${POSTGRES_USER:-cashguard}"
REDIS_PASSWORD="${REDIS_PASSWORD:-cashguard_redis}"
APP_MODE_VALUE="${APP_MODE:-mock}"
ADMIN_EMAIL_VALUE="${ADMIN_EMAIL:-admin@localhost}"
T212_KEY_VALUE="${T212_API_KEY:-}"

# - Ensure Docker is running --------------------------------------------------
step "Checking Docker..."
if ! docker info >/dev/null 2>&1; then
    echo "    Docker not running - starting Docker Desktop..."
    open -a Docker
    for i in $(seq 1 60); do
        sleep 2
        if docker info >/dev/null 2>&1; then
            break
        fi
        if [ "$i" = "60" ]; then
            fail "Docker failed to start. Open Docker Desktop manually."
            read -p "  Press ENTER to close..."
            exit 1
        fi
    done
fi
ok "Docker running"

# - Start database services ---------------------------------------------------
step "Starting Postgres + Redis..."
if ! compose -f "$PROJECT_ROOT/docker-compose.yml" up -d postgres redis >/tmp/cashguard-start-compose.log 2>&1; then
    fail "Could not start Postgres + Redis"
    tail -20 /tmp/cashguard-start-compose.log | sed 's/^/    /'
    read -p "  Press ENTER to close..."
    exit 1
fi
tail -5 /tmp/cashguard-start-compose.log | sed 's/^/    /'

step "Waiting for PostgreSQL..."
POSTGRES_READY=false
for i in $(seq 1 30); do
    if compose -f "$PROJECT_ROOT/docker-compose.yml" exec -T postgres pg_isready -U "$POSTGRES_USER" >/dev/null 2>&1; then
        POSTGRES_READY=true
        break
    fi
    sleep 1
done
if [ "$POSTGRES_READY" != true ]; then
    fail "PostgreSQL did not become ready"
    read -p "  Press ENTER to close..."
    exit 1
fi
ok "PostgreSQL ready"

step "Waiting for Redis..."
REDIS_READY=false
for i in $(seq 1 30); do
    if docker exec t212_redis redis-cli -a "$REDIS_PASSWORD" ping >/dev/null 2>&1; then
        REDIS_READY=true
        break
    fi
    sleep 1
done
if [ "$REDIS_READY" != true ]; then
    fail "Redis did not become ready"
    read -p "  Press ENTER to close..."
    exit 1
fi
ok "Redis ready"

# - Apply migrations ----------------------------------------------------------
step "Applying database updates..."
cd "$PROJECT_ROOT/apps/api"
export DATABASE_URL="${DATABASE_URL:-postgresql+asyncpg://cashguard:cashguard_secret@localhost:5432/cashguard}"
if MIGRATION_OUTPUT=$("$PYTHON" -m alembic upgrade head 2>&1); then
    echo "$MIGRATION_OUTPUT" | grep -E "Running|already|head|upgrade" | sed 's/^/    /'
    ok "Database schema is up to date"
else
    fail "Database migration failed"
    echo "$MIGRATION_OUTPUT" | tail -12 | sed 's/^/    /'
    read -p "  Press ENTER to close..."
    exit 1
fi

# - Write frontend runtime env ------------------------------------------------
cat > "$PROJECT_ROOT/apps/web/.env.local" << ENVLOCAL
NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_APP_MODE=${APP_MODE_VALUE}
NEXT_PUBLIC_ENABLE_PWA=false
ENVLOCAL
ok "Frontend runtime config refreshed"

# - Ensure frontend dependencies exist ---------------------------------------
if [ ! -d "$PROJECT_ROOT/apps/web/node_modules" ]; then
    step "Installing frontend dependencies..."
    cd "$PROJECT_ROOT/apps/web"
    if ! "$NPM" install --no-fund --no-audit >/tmp/cashguard-web-install.log 2>&1; then
        fail "npm install failed"
        tail -20 /tmp/cashguard-web-install.log | sed 's/^/    /'
        read -p "  Press ENTER to close..."
        exit 1
    fi
    ok "Frontend dependencies installed"
fi

# - Stop anything already on our ports ---------------------------------------
step "Clearing old local processes on ports 8000 and 3000..."
stop_port_processes 8000
stop_port_processes 3000
sleep 1
ok "Ports 8000 and 3000 are free"

mkdir -p "$LOG_DIR"

# - Start API -----------------------------------------------------------------
step "Starting API server..."
cd "$PROJECT_ROOT/apps/api"
"$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
    > "$LOG_DIR/api.log" 2>&1 &
API_PID=$!

if ! wait_for_url "http://localhost:8000/v1/health/live" 30; then
    fail "API failed to start"
    tail -20 "$LOG_DIR/api.log" 2>/dev/null | sed 's/^/    /'
    read -p "  Press ENTER to close..."
    exit 1
fi
ok "API server running (PID $API_PID)"

# - Start frontend ------------------------------------------------------------
step "Starting frontend..."
cd "$PROJECT_ROOT/apps/web"
"$NPM" run dev > "$LOG_DIR/web.log" 2>&1 &
WEB_PID=$!

if ! wait_for_url "http://localhost:3000/auth/login" 45; then
    fail "Frontend failed to start"
    tail -20 "$LOG_DIR/web.log" 2>/dev/null | sed 's/^/    /'
    read -p "  Press ENTER to close..."
    exit 1
fi

if ! curl -sf "http://localhost:3000/_next/static/chunks/webpack.js" >/dev/null 2>&1; then
    warn "Frontend is up, but the main webpack chunk is not reachable yet"
    echo "    If the browser looks blank, wait a few seconds and refresh once."
fi
ok "Frontend running (PID $WEB_PID)"

# - Start workers -------------------------------------------------------------
step "Starting automation workers..."
cd "$PROJECT_ROOT/apps/api"
"$PYTHON" -m celery -A app.workers.celery_app worker \
    --beat --loglevel=warning --concurrency=2 \
    > "$LOG_DIR/celery.log" 2>&1 &
CELERY_PID=$!
sleep 3
if ! kill -0 "$CELERY_PID" 2>/dev/null; then
    fail "Automation workers failed to start"
    tail -20 "$LOG_DIR/celery.log" 2>/dev/null | sed 's/^/    /'
    read -p "  Press ENTER to close..."
    exit 1
fi
ok "Automation workers running (PID $CELERY_PID)"

echo "$API_PID $WEB_PID $CELERY_PID" > "$PROJECT_ROOT/.pids"

# - Open browser --------------------------------------------------------------
if [ "${CASHGUARD_NO_OPEN:-0}" != "1" ]; then
    sleep 2
    open "http://localhost:3000"
fi

clear
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║              T212 CashGuard Trader - Running                 ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  Browser:   http://localhost:3000"
echo "  API docs:  http://localhost:8000/docs"
echo "  Logs:      $PROJECT_ROOT/logs/"
echo ""

echo "  Login:     $ADMIN_EMAIL_VALUE"
if [ -n "${ADMIN_PASSWORD:-}" ]; then
    echo "  Password:  ${ADMIN_PASSWORD}"
fi
echo "  Mode:      $APP_MODE_VALUE"
[ -z "$T212_KEY_VALUE" ] && echo -e "  ${YELLOW}⚠  No Trading 212 key - running in mock mode${RESET}"

echo ""
echo -e "  ${BOLD}Keep this window open while the app is running.${RESET}"
echo -e "  To stop everything, close this window or press ${BOLD}Ctrl+C${RESET}."
if [ "${CASHGUARD_NO_OPEN:-0}" = "1" ]; then
    echo "  Browser opening skipped because CASHGUARD_NO_OPEN=1"
fi
echo ""
echo "  ---------------------------------------------------------"
echo "  Workers log (last 5 lines):"
tail -5 "$LOG_DIR/celery.log" 2>/dev/null | sed 's/^/    /'
echo ""

while true; do
    sleep 30

    if [ "$SHUTDOWN_REQUESTED" = "1" ]; then
        break
    fi

    if ! kill -0 "$API_PID" 2>/dev/null; then
        echo -e "\n  ${RED}⚠  API server stopped unexpectedly - restarting...${RESET}"
        # Clear any lingering socket on port 8000 (TIME_WAIT or orphan child)
        # before respawning, otherwise uvicorn hits [Errno 48] in a hot loop.
        stop_port_processes 8000
        cd "$PROJECT_ROOT/apps/api"
        "$PYTHON" -m uvicorn app.main:app --host 0.0.0.0 --port 8000 \
            >> "$LOG_DIR/api.log" 2>&1 &
        API_PID=$!
        if wait_for_url "http://localhost:8000/v1/health/live" 30; then
            ok "API server restarted"
        else
            fail "API restart failed - check $LOG_DIR/api.log"
        fi
    fi

    if ! kill -0 "$WEB_PID" 2>/dev/null; then
        echo -e "\n  ${RED}⚠  Frontend stopped unexpectedly - restarting...${RESET}"
        # Same precaution for port 3000 — next dev won't rebind over a stale socket.
        stop_port_processes 3000
        cd "$PROJECT_ROOT/apps/web"
        "$NPM" run dev >> "$LOG_DIR/web.log" 2>&1 &
        WEB_PID=$!
        if wait_for_url "http://localhost:3000/auth/login" 45; then
            ok "Frontend restarted"
        else
            fail "Frontend restart failed - check $LOG_DIR/web.log"
        fi
    fi

    if ! kill -0 "$CELERY_PID" 2>/dev/null; then
        echo -e "\n  ${RED}⚠  Workers stopped - restarting...${RESET}"
        cd "$PROJECT_ROOT/apps/api"
        "$PYTHON" -m celery -A app.workers.celery_app worker \
            --beat --loglevel=warning --concurrency=2 \
            >> "$LOG_DIR/celery.log" 2>&1 &
        CELERY_PID=$!
        sleep 3
        if kill -0 "$CELERY_PID" 2>/dev/null; then
            ok "Workers restarted"
        else
            fail "Worker restart failed - check $LOG_DIR/celery.log"
        fi
    fi

    echo "$API_PID $WEB_PID $CELERY_PID" > "$PROJECT_ROOT/.pids"
done
