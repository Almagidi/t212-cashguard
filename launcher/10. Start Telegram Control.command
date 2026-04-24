#!/bin/bash
# ============================================================
# CashGuard - Start Telegram Control Tunnel
# Starts a public HTTPS tunnel and registers the Telegram webhook.
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

validate_bot_token() {
    local response
    response="$(curl -sS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" || true)"
    if printf '%s' "$response" | grep -q '"ok":true'; then
        return 0
    fi

    fail "TELEGRAM_BOT_TOKEN was rejected by Telegram"
    echo "  Recheck the rotated token from BotFather and update .env before retrying."
    printf '%s\n' "$response" | sed 's/^/    /'
    return 1
}

STATE_DIR="$PROJECT_ROOT/.telegram-control"
PID_FILE="$STATE_DIR/tunnel.pid"
URL_FILE="$STATE_DIR/tunnel.url"
LOG_FILE="$PROJECT_ROOT/logs/telegram-tunnel.log"
RUNTIME_DIR="$STATE_DIR/runtime"
LOCALTUNNEL_BIN="$RUNTIME_DIR/node_modules/localtunnel/bin/lt.js"

mkdir -p "$STATE_DIR" "$PROJECT_ROOT/logs"

TUNNEL_PID=""
STARTED_WEBHOOK=0

cleanup() {
    if [ "$STARTED_WEBHOOK" = "1" ] && [ -n "${TELEGRAM_BOT_TOKEN:-}" ]; then
        curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/deleteWebhook" >/dev/null 2>&1 || true
    fi

    if [ -n "$TUNNEL_PID" ] && kill -0 "$TUNNEL_PID" 2>/dev/null; then
        kill "$TUNNEL_PID" 2>/dev/null || true
        wait "$TUNNEL_PID" 2>/dev/null || true
    fi

    rm -f "$PID_FILE" "$URL_FILE"
}

trap cleanup EXIT
trap 'exit 0' INT TERM

clear
echo ""
echo -e "${BOLD}${BLUE}  CashGuard Telegram Control${RESET}   Starting tunnel..."
echo ""

if [ ! -f "$PROJECT_ROOT/.env" ]; then
    fail ".env file not found"
    read -p "  Press ENTER to close..." || true
    exit 1
fi

set -a
# shellcheck disable=SC1090
source "$PROJECT_ROOT/.env"
set +a

if [ -z "${TELEGRAM_BOT_TOKEN:-}" ]; then
    fail "TELEGRAM_BOT_TOKEN is missing"
    read -p "  Press ENTER to close..." || true
    exit 1
fi

if [ -z "${TELEGRAM_WEBHOOK_SECRET:-}" ]; then
    fail "TELEGRAM_WEBHOOK_SECRET is missing"
    read -p "  Press ENTER to close..." || true
    exit 1
fi

step "Validating Telegram bot token..."
if ! validate_bot_token; then
    read -p "  Press ENTER to close..." || true
    exit 1
fi
ok "Telegram bot token is valid"

if ! curl -sf http://127.0.0.1:8000/v1/health/live >/dev/null 2>&1; then
    fail "CashGuard API is not running on localhost:8000"
    echo "  Start the main app first with '2. Start CashGuard.command'."
    read -p "  Press ENTER to close..." || true
    exit 1
fi
ok "CashGuard API is reachable"

if ! command -v npm >/dev/null 2>&1; then
    fail "npm is required to launch the tunnel"
    read -p "  Press ENTER to close..." || true
    exit 1
fi

if ! command -v node >/dev/null 2>&1; then
    if [ ! -x "/opt/homebrew/bin/cloudflared" ]; then
        fail "node or cloudflared is required to launch the tunnel"
        read -p "  Press ENTER to close..." || true
        exit 1
    fi
fi

if [ -f "$PID_FILE" ]; then
    OLD_PID="$(cat "$PID_FILE" 2>/dev/null || true)"
    if [ -n "$OLD_PID" ] && kill -0 "$OLD_PID" 2>/dev/null; then
        step "Stopping previous Telegram tunnel..."
        kill "$OLD_PID" 2>/dev/null || true
        sleep 1
        if kill -0 "$OLD_PID" 2>/dev/null; then
            kill -9 "$OLD_PID" 2>/dev/null || true
        fi
        ok "Old tunnel stopped"
    fi
fi

rm -f "$PID_FILE" "$URL_FILE"
: > "$LOG_FILE"

PUBLIC_URL=""

if [ ! -f "$LOCALTUNNEL_BIN" ]; then
    step "Installing localtunnel runtime..."
    mkdir -p "$RUNTIME_DIR"
    if ! npm install --prefix "$RUNTIME_DIR" --no-save localtunnel >/tmp/cashguard-telegram-install.log 2>&1; then
        fail "Could not install localtunnel"
        tail -20 /tmp/cashguard-telegram-install.log | sed 's/^/    /'
        read -p "  Press ENTER to close..." || true
        exit 1
    fi
    ok "localtunnel runtime installed"
fi

step "Starting localtunnel..."
node "$LOCALTUNNEL_BIN" --port 8000 --print-requests >"$LOG_FILE" 2>&1 &
TUNNEL_PID=$!
echo "$TUNNEL_PID" > "$PID_FILE"

for _ in $(seq 1 30); do
    if [ ! -f "$LOG_FILE" ]; then
        sleep 1
        continue
    fi
    PUBLIC_URL="$(sed -n 's/^your url is: //p' "$LOG_FILE" | tail -1)"
    if [ -n "$PUBLIC_URL" ]; then
        break
    fi
    if ! kill -0 "$TUNNEL_PID" 2>/dev/null; then
        break
    fi
    sleep 1
done

if [ -z "$PUBLIC_URL" ]; then
    fail "Could not obtain a public tunnel URL"
    tail -20 "$LOG_FILE" | sed 's/^/    /'
    read -p "  Press ENTER to close..." || true
    exit 1
fi

echo "$PUBLIC_URL" > "$URL_FILE"
ok "Tunnel ready: $PUBLIC_URL"

WEBHOOK_URL="${PUBLIC_URL%/}/v1/telegram/webhook"

step "Registering Telegram webhook..."
WEBHOOK_RESPONSE=""
WEBHOOK_OK=0
for _ in $(seq 1 15); do
    WEBHOOK_RESPONSE="$(curl -sS -X POST "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/setWebhook" \
        -d "url=${WEBHOOK_URL}" \
        -d "secret_token=${TELEGRAM_WEBHOOK_SECRET}" \
        -d 'allowed_updates=["message","edited_message"]' || true)"
    if printf '%s' "$WEBHOOK_RESPONSE" | grep -q '"ok":true'; then
        WEBHOOK_OK=1
        break
    fi
    sleep 2
done

if [ "$WEBHOOK_OK" != "1" ]; then
    fail "Telegram webhook registration failed"
    printf '%s\n' "$WEBHOOK_RESPONSE" | sed 's/^/    /'
    read -p "  Press ENTER to close..." || true
    exit 1
fi
ok "Telegram webhook registered"
STARTED_WEBHOOK=1

echo ""
echo -e "${BOLD}${GREEN}  Telegram control is live.${RESET}"
echo ""
echo "  Public webhook: $WEBHOOK_URL"
echo "  Tunnel log:     $LOG_FILE"
echo ""
echo "  You can now message your bot from iPhone with commands like:"
echo "    /status"
echo "    /positions"
echo "    /pause"
echo ""
echo "  Keep this window open while Telegram control is enabled."
echo "  Press Ctrl+C to stop the tunnel and remove the webhook."
echo ""

while kill -0 "$TUNNEL_PID" 2>/dev/null; do
    sleep 5
done

warn "Telegram control tunnel stopped."
exit 0
