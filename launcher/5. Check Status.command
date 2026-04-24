#!/bin/bash
# ============================================================
# T212 CashGuard Trader - Status Check
# Shows what's running, any errors, and recent activity.
# ============================================================

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'; BLUE='\033[0;34m'

ok()    { echo -e "  ${GREEN}● RUNNING${RESET}  $1"; }
stop()  { echo -e "  ${RED}● STOPPED${RESET}  $1"; }
warn()  { echo -e "  ${YELLOW}● WARNING${RESET}  $1"; }
info()  { echo -e "  ${CYAN}● INFO${RESET}     $1"; }

print_log_excerpt() {
    local log_file="$1"
    python3 - "$log_file" <<'PY'
from pathlib import Path
import sys

path = Path(sys.argv[1])
try:
    lines = path.read_text(errors="replace").splitlines()[-5:]
except OSError:
    print("      (Unable to read log file)")
    raise SystemExit(0)

if not lines:
    print("      (Log file is empty)")
    raise SystemExit(0)

for line in lines:
    print(f"      {line}")
PY
}

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

if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$PROJECT_ROOT/.env"
    set +a
fi

POSTGRES_USER="${POSTGRES_USER:-cashguard}"
REDIS_PASSWORD="${REDIS_PASSWORD:-cashguard_redis}"
TELEGRAM_TUNNEL_PID_FILE="$PROJECT_ROOT/.telegram-control/tunnel.pid"
TELEGRAM_TUNNEL_URL_FILE="$PROJECT_ROOT/.telegram-control/tunnel.url"

clear
echo ""
echo -e "${BOLD}${BLUE}  T212 CashGuard Trader - Status${RESET}   $(date '+%H:%M:%S')"
echo ""
echo "  Services:"

if curl -sf http://localhost:8000/v1/health/live >/dev/null 2>&1; then
    ok "API server (port 8000)"
else
    stop "API server (port 8000)"
fi

if curl -sf http://localhost:3000/auth/login >/dev/null 2>&1; then
    ok "Frontend (port 3000)"
else
    stop "Frontend (port 3000)"
fi

if [ -n "$COMPOSE_CMD" ] && compose -f "$PROJECT_ROOT/docker-compose.yml" exec -T postgres pg_isready -U "$POSTGRES_USER" >/dev/null 2>&1; then
    ok "PostgreSQL database"
else
    stop "PostgreSQL database"
fi

if docker exec t212_redis redis-cli -a "$REDIS_PASSWORD" ping >/dev/null 2>&1; then
    ok "Redis cache"
else
    stop "Redis cache"
fi

if pgrep -f "$PROJECT_ROOT/launcher/2. Start CashGuard.command" >/dev/null 2>&1; then
    ok "Start launcher supervisor"
else
    warn "Start launcher supervisor not detected"
fi

if [ -f "$PROJECT_ROOT/.pids" ]; then
    read -r API_PID WEB_PID CELERY_PID < "$PROJECT_ROOT/.pids" || true
    if [ -n "${CELERY_PID:-}" ] && kill -0 "$CELERY_PID" 2>/dev/null; then
        ok "Automation workers"
    else
        stop "Automation workers"
    fi
else
    warn "Automation workers unknown (no .pids file)"
fi

if [ -f "$TELEGRAM_TUNNEL_PID_FILE" ]; then
    TUNNEL_PID="$(cat "$TELEGRAM_TUNNEL_PID_FILE" 2>/dev/null || true)"
    if [ -n "${TUNNEL_PID:-}" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        ok "Telegram control tunnel"
    else
        warn "Telegram control tunnel state exists but process is not running"
    fi
fi

echo ""
echo "  Configuration:"
if [ -f "$PROJECT_ROOT/.env" ]; then
    MODE="${APP_MODE:-mock}"
    T212="${T212_API_KEY:-}"
    POLY="${POLYGON_API_KEY:-}"
    T212_ENV="${T212_ENVIRONMENT:-demo}"

    case "$MODE" in
        live) echo -e "  Mode:         ${RED}${BOLD}LIVE - REAL MONEY${RESET}" ;;
        demo) echo -e "  Mode:         ${YELLOW}DEMO (fake money, real API)${RESET}" ;;
        *)    echo -e "  Mode:         ${CYAN}MOCK (simulated data)${RESET}" ;;
    esac

    [ -n "$T212" ] && \
        echo "  Trading 212:  ✓ Key set (${T212_ENV} environment)" || \
        echo -e "  Trading 212:  ${YELLOW}⚠  Not configured${RESET}"
    [ -n "$POLY" ] && \
        echo "  Polygon.io:   ✓ Key set" || \
        echo -e "  Polygon.io:   ${YELLOW}⚠  Not configured (using simulated/backfill data)${RESET}"
else
    echo "  .env file not found"
fi

echo ""
echo "  Health check:"
HEALTH=$(curl -sf http://localhost:8000/v1/health/deps 2>/dev/null || true)
if [ -n "$HEALTH" ]; then
    echo "$HEALTH" | python3 -c "
import json, sys
data = json.load(sys.stdin)
for k, v in data.items():
    state = str(v)
    if state in ('ok', 'connected', 'pass', 'mock', 'polygon', 'alpaca_realtime', 'unknown'):
        colour = '\033[32m'
    elif state in ('credentials_invalid', 'stale'):
        colour = '\033[33m'
    else:
        colour = '\033[31m'
    print(f'  {colour}{k:<15}\033[0m {state}')
" 2>/dev/null || echo "  (Unable to parse health response)"
else
    echo "  (API not reachable)"
fi

echo ""
echo "  Launcher PIDs:"
if [ -f "$PROJECT_ROOT/.pids" ]; then
    read -r API_PID WEB_PID CELERY_PID < "$PROJECT_ROOT/.pids" || true
    echo "    API:      ${API_PID:-unknown}"
    echo "    Frontend: ${WEB_PID:-unknown}"
    echo "    Workers:  ${CELERY_PID:-unknown}"
else
    echo "    (No .pids file found)"
fi

if [ -f "$TELEGRAM_TUNNEL_URL_FILE" ]; then
    echo "    Telegram: $(cat "$TELEGRAM_TUNNEL_URL_FILE" 2>/dev/null)"
fi

echo ""
echo "  Recent activity:"
for log_name in api.log web.log celery.log; do
    LOG_FILE="$PROJECT_ROOT/logs/$log_name"
    echo "    ${log_name}:"
    if [ -f "$LOG_FILE" ]; then
        print_log_excerpt "$LOG_FILE"
    else
        echo "      (No log file found)"
    fi
done

echo ""
echo "  Dashboard: http://localhost:3000"
echo "  API docs:  http://localhost:8000/docs"
echo "  Logs:      $PROJECT_ROOT/logs/"
echo ""
read -p "  Press ENTER to close..." || true
