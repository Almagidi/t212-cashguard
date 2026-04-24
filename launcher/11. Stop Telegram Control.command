#!/bin/bash
# ============================================================
# CashGuard - Stop Telegram Control Tunnel
# Removes Telegram webhook and stops the localtunnel process.
# ============================================================

set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'
CYAN='\033[0;36m'; BOLD='\033[1m'; BLUE='\033[0;34m'; RESET='\033[0m'

ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; }
step() { echo -e "  ${CYAN}▸${RESET} $1"; }

STATE_DIR="$PROJECT_ROOT/.telegram-control"
PID_FILE="$STATE_DIR/tunnel.pid"
URL_FILE="$STATE_DIR/tunnel.url"

clear
echo ""
echo -e "${BOLD}${BLUE}  CashGuard Telegram Control${RESET}   Stopping tunnel..."
echo ""

if [ -f "$PROJECT_ROOT/.env" ]; then
    set -a
    # shellcheck disable=SC1090
    source "$PROJECT_ROOT/.env"
    set +a
fi

if [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
    step "Deleting Telegram webhook..."
    WEBHOOK_RESPONSE="$(curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook" || true)"
    if printf '%s' "$WEBHOOK_RESPONSE" | grep -q '"ok":true'; then
        ok "Telegram webhook removed"
    else
        warn "Could not confirm webhook deletion"
        printf '%s\n' "$WEBHOOK_RESPONSE" | sed 's/^/    /'
    fi
else
    warn "TELEGRAM_BOT_TOKEN missing, skipping webhook deletion"
fi

if [ -f "$PID_FILE" ]; then
    TUNNEL_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$TUNNEL_PID" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        step "Stopping localtunnel process..."
        kill "$TUNNEL_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$TUNNEL_PID" 2>/dev/null; then
            kill -9 "$TUNNEL_PID" 2>/dev/null || true
        fi
        ok "Telegram tunnel stopped"
    else
        warn "No running tunnel process found"
    fi
else
    warn "No Telegram tunnel state file found"
fi

rm -f "$PID_FILE" "$URL_FILE"

echo ""
echo -e "  ${BOLD}Telegram control tunnel stopped.${RESET}"
echo ""
read -p "  Press ENTER to close..." || true
