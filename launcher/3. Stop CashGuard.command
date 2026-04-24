#!/bin/bash
# ============================================================
# T212 CashGuard Trader - Stop All Services
# ============================================================

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
BOLD='\033[1m'; CYAN='\033[0;36m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; }
step() { echo -e "  ${CYAN}▸${RESET} $1"; }

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

stop_pid_if_running() {
    local pid="$1"
    local label="$2"

    [ -z "$pid" ] && return 0
    if ! kill -0 "$pid" 2>/dev/null; then
        return 0
    fi

    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 10); do
        if ! kill -0 "$pid" 2>/dev/null; then
            ok "$label stopped (PID $pid)"
            return 0
        fi
        sleep 1
    done

    kill -9 "$pid" 2>/dev/null || true
    if ! kill -0 "$pid" 2>/dev/null; then
        ok "$label force-stopped (PID $pid)"
    else
        warn "Could not stop $label cleanly (PID $pid)"
    fi
}

stop_port_listener() {
    local port="$1"
    local pids

    pids=$(lsof -tiTCP:"$port" -sTCP:LISTEN 2>/dev/null || true)
    [ -z "$pids" ] && return 0

    echo "$pids" | while read -r pid; do
        [ -z "$pid" ] && continue
        kill "$pid" 2>/dev/null || true
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    done
}

clear
echo ""
echo -e "${BOLD}  T212 CashGuard Trader - Stopping all services...${RESET}"
echo ""

step "Stopping launcher supervisors..."
LAUNCHER_PIDS=$(pgrep -f "$PROJECT_ROOT/launcher/2. Start CashGuard.command" 2>/dev/null || true)
if [ -n "$LAUNCHER_PIDS" ]; then
    echo "$LAUNCHER_PIDS" | while read -r pid; do
        [ -z "$pid" ] && continue
        kill "$pid" 2>/dev/null || true
        sleep 1
        if kill -0 "$pid" 2>/dev/null; then
            kill -9 "$pid" 2>/dev/null || true
        fi
        ok "Start launcher stopped (PID $pid)"
    done
else
    warn "No running start-launcher supervisor found"
fi

step "Stopping saved service PIDs..."
if [ -f "$PROJECT_ROOT/.pids" ]; then
    read -r API_PID WEB_PID CELERY_PID < "$PROJECT_ROOT/.pids" || true
    stop_pid_if_running "${API_PID:-}" "API server"
    stop_pid_if_running "${WEB_PID:-}" "Frontend"
    stop_pid_if_running "${CELERY_PID:-}" "Workers"
    rm -f "$PROJECT_ROOT/.pids"
else
    warn "No .pids file found - falling back to port/process checks"
fi

step "Clearing any remaining listeners..."
stop_port_listener 8000
stop_port_listener 3000
ok "Ports 8000 and 3000 cleared"

TELEGRAM_TUNNEL_PID_FILE="$PROJECT_ROOT/.telegram-control/tunnel.pid"
if [ -f "$TELEGRAM_TUNNEL_PID_FILE" ]; then
    step "Stopping Telegram control tunnel..."
    TUNNEL_PID="$(cat "$TELEGRAM_TUNNEL_PID_FILE" 2>/dev/null || true)"
    if [ -n "$TUNNEL_PID" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        kill "$TUNNEL_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$TUNNEL_PID" 2>/dev/null; then
            kill -9 "$TUNNEL_PID" 2>/dev/null || true
        fi
        ok "Telegram control tunnel stopped"
    else
        warn "Telegram tunnel state existed but no process was running"
    fi
    rm -f "$PROJECT_ROOT/.telegram-control/tunnel.pid" "$PROJECT_ROOT/.telegram-control/tunnel.url"
fi

step "Stopping Postgres + Redis..."
if [ -n "$COMPOSE_CMD" ]; then
    if compose -f "$PROJECT_ROOT/docker-compose.yml" stop postgres redis >/tmp/cashguard-stop-compose.log 2>&1; then
        tail -5 /tmp/cashguard-stop-compose.log | sed 's/^/    /'
        ok "Database services stopped (data preserved)"
    else
        warn "Docker stop reported an issue"
        tail -10 /tmp/cashguard-stop-compose.log | sed 's/^/    /'
    fi
else
    warn "Docker Compose not available - skipped database stop"
fi

echo ""
echo -e "  ${BOLD}All services stopped.${RESET}"
echo "  Your data is safely stored and will be there next time."
echo ""
read -p "  Press ENTER to close..." || true
