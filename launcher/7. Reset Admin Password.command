#!/bin/bash
# Reset admin password to match what's in .env
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

GREEN='\033[0;32m'; RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'

echo ""
echo -e "${BOLD}  T212 CashGuard — Admin Password Reset${RESET}"
echo "  ─────────────────────────────────────────"
echo ""

PYTHON="$PROJECT_ROOT/venv/bin/python"
if [ ! -f "$PYTHON" ]; then
    PYTHON=$(which python3.12 2>/dev/null || which python3.11 2>/dev/null || which python3 2>/dev/null)
fi

cd "$PROJECT_ROOT/apps/api"

$PYTHON "$PROJECT_ROOT/reset_password.py"
STATUS=$?

if [ $STATUS -eq 0 ]; then
    PW=$(grep "^ADMIN_PASSWORD=" "$PROJECT_ROOT/.env" | cut -d= -f2)
    echo ""
    echo -e "${GREEN}  ✓ Done! Login at http://localhost:3000${RESET}"
    echo ""
    echo "  Email:    admin@localhost"
    echo "  Password: $PW"
else
    echo ""
    echo -e "${RED}  ✗ Reset failed — check that the app is running${RESET}"
fi

echo ""
read -p "  Press ENTER to close..."
