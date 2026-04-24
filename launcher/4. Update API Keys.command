#!/bin/bash
# ============================================================
# T212 CashGuard Trader — Update API Keys
# ============================================================

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'
BOLD='\033[1m'; RESET='\033[0m'; BLUE='\033[0;34m'; RED='\033[0;31m'

update_env() {
    local KEY="$1"; local VALUE="$2"
    if grep -q "^${KEY}=" .env 2>/dev/null; then
        sed -i.bak "s|^${KEY}=.*|${KEY}=${VALUE}|" .env && rm -f .env.bak
    else
        echo "${KEY}=${VALUE}" >> .env
    fi
}

clear
echo ""
echo -e "${BOLD}${BLUE}  T212 CashGuard — Update API Keys${RESET}"
echo ""

echo "  Current configuration:"
if [ -f ".env" ]; then
    T212=$(grep "^T212_API_KEY=" .env | cut -d= -f2)
    ALPACA=$(grep "^ALPACA_API_KEY=" .env | cut -d= -f2)
    POLY=$(grep "^POLYGON_API_KEY=" .env | cut -d= -f2)
    MODE=$(grep "^APP_MODE=" .env | cut -d= -f2)
    
    [ -n "$T212" ]    && echo "  Trading 212: ${T212:0:8}••••••••" \
                      || echo -e "  Trading 212: ${YELLOW}(not set)${RESET}"
    [ -n "$ALPACA" ]  && echo "  Alpaca:      ${ALPACA:0:8}••••••• (real-time data)" \
                      || echo -e "  Alpaca:      ${YELLOW}(not set — signals will be delayed or simulated)${RESET}"
    [ -n "$POLY" ]    && echo "  Polygon:     ${POLY:0:8}•••••••  (backtesting)" \
                      || echo -e "  Polygon:     ${YELLOW}(not set — backtesting unavailable)${RESET}"
    echo "  Mode:        $MODE"
fi

echo ""
echo "  What would you like to update?"
echo "  1) Trading 212 API Key & Secret"
echo "  2) Alpaca API Keys (real-time data — recommended)"
echo "  3) Polygon.io API Key (backtesting only)"
echo "  4) All three"
echo "  5) Cancel"
echo ""
read -p "  Enter choice (1-5): " CHOICE

case "$CHOICE" in
    1|4)
        echo ""
        echo "  Trading 212: Settings → API → Generate API Key"
        read -p "  New Trading 212 API Key: " T212_KEY
        read -p "  New Trading 212 API Secret: " T212_SECRET
        if [ -n "$T212_KEY" ]; then
            update_env "T212_API_KEY" "$T212_KEY"
            update_env "T212_API_SECRET" "$T212_SECRET"
            update_env "APP_MODE" "demo"
            update_env "NEXT_PUBLIC_APP_MODE" "demo"
            echo -e "\n  ${GREEN}✓${RESET} Trading 212 keys updated"
        fi
        ;;&

    2|4)
        echo ""
        echo -e "  ${BOLD}Alpaca free real-time data setup:${RESET}"
        echo "  1. Go to https://alpaca.markets"
        echo "  2. Create free account → choose Paper Trading"
        echo "  3. API Keys → Generate New Key"
        echo ""
        read -p "  Open Alpaca in browser? (Y/n): " OPEN_ALP
        [[ "$OPEN_ALP" != "n" ]] && open "https://app.alpaca.markets/paper-trading/overview"
        echo ""
        read -p "  Alpaca API Key ID: " ALPACA_KEY
        read -p "  Alpaca Secret Key: " ALPACA_SECRET
        if [ -n "$ALPACA_KEY" ] && [ -n "$ALPACA_SECRET" ]; then
            update_env "ALPACA_API_KEY" "$ALPACA_KEY"
            update_env "ALPACA_API_SECRET" "$ALPACA_SECRET"
            update_env "MARKET_DATA_PROVIDER" "alpaca"
            echo -e "\n  ${GREEN}✓${RESET} Alpaca keys updated — real-time data enabled"
        fi
        ;;&

    3|4)
        echo ""
        echo "  Polygon is used for backtesting (not live signals)."
        echo "  Get free key at https://polygon.io"
        read -p "  Open Polygon in browser? (Y/n): " OPEN_POLY
        [[ "$OPEN_POLY" != "n" ]] && open "https://polygon.io/dashboard"
        echo ""
        read -p "  New Polygon.io API Key: " POLY_KEY
        if [ -n "$POLY_KEY" ]; then
            update_env "POLYGON_API_KEY" "$POLY_KEY"
            echo -e "\n  ${GREEN}✓${RESET} Polygon key updated — backtesting enabled"
        fi
        ;;

    5) echo "  Cancelled."; read -p "  Press ENTER to close..."; exit 0 ;;
esac

echo ""
echo -e "  ${BOLD}Keys updated.${RESET}"
echo "  Restart CashGuard for changes to take effect:"
echo "  → Run '3. Stop CashGuard.command' then '2. Start CashGuard.command'"
echo ""
read -p "  Press ENTER to close..."
