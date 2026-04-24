#!/bin/bash
# ============================================================
# T212 CashGuard Trader — Enable Live Trading
# WARNING: This switches to real money.
# Only run this after 30+ days in demo mode.
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BOLD='\033[1m'; RESET='\033[0m'

clear
echo ""
echo -e "${BOLD}${RED}╔══════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${RED}║               ⚠  LIVE TRADING — REAL MONEY  ⚠               ║${RESET}"
echo -e "${BOLD}${RED}╚══════════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo -e "${BOLD}  This will switch the app to use REAL money from your Trading 212${RESET}"
echo -e "${BOLD}  live account. Losses will be real.${RESET}"
echo ""
echo "  Before proceeding, confirm ALL of these:"
echo ""
echo "  [ ] I have run a backtest showing Sharpe ≥ 1.0 and ≥ 30 trades"
echo "  [ ] I have used demo mode for at least 30 trading days"
echo "  [ ] I have a live Trading 212 API key ready"
echo "  [ ] I accept that I may lose money and this is not financial advice"
echo "  [ ] I have set appropriate daily loss limits in the Risk settings"
echo ""

read -p "  Type YES to continue, or anything else to cancel: " CONFIRM
if [ "$CONFIRM" != "YES" ]; then
    echo ""
    echo "  Cancelled — staying in demo mode."
    echo ""
    read -p "  Press ENTER to close..."
    exit 0
fi

echo ""
read -p "  Your live Trading 212 API Key: " LIVE_KEY
read -p "  Your live Trading 212 API Secret: " LIVE_SECRET

if [ -z "$LIVE_KEY" ] || [ -z "$LIVE_SECRET" ]; then
    echo ""
    echo -e "${RED}  No keys entered — cancelled.${RESET}"
    read -p "  Press ENTER to close..."
    exit 1
fi

# Update .env
update_env() {
    local KEY="$1"; local VALUE="$2"
    if grep -q "^${KEY}=" .env; then
        sed -i.bak "s|^${KEY}=.*|${KEY}=${VALUE}|" .env && rm -f .env.bak
    else
        echo "${KEY}=${VALUE}" >> .env
    fi
}

update_env "T212_API_KEY" "$LIVE_KEY"
update_env "T212_API_SECRET" "$LIVE_SECRET"
update_env "T212_ENVIRONMENT" "live"
update_env "APP_MODE" "live"
update_env "NEXT_PUBLIC_APP_MODE" "live"
update_env "LIVE_TRADING_ENABLED" "true"

echo ""
echo -e "${BOLD}${RED}  ⚠  LIVE MODE ENABLED ⚠${RESET}"
echo ""
echo "  Restart CashGuard to apply changes:"
echo "  1. Run '3. Stop CashGuard.command'"
echo "  2. Run '2. Start CashGuard.command'"
echo ""
echo "  The dashboard will show a red LIVE badge."
echo "  You can switch back to demo at any time using '4. Update API Keys.command'"
echo ""
read -p "  Press ENTER to close..."
