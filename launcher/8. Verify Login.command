#!/bin/bash
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GREEN='\033[0;32m'; RED='\033[0;31m'; BOLD='\033[1m'; RESET='\033[0m'

PW=$(grep "^ADMIN_PASSWORD=" "$PROJECT_ROOT/.env" | cut -d= -f2)

echo ""
echo -e "${BOLD}  CashGuard — Login Verification${RESET}"
echo "  ─────────────────────────────────────────"
echo "  Testing: admin@localhost / $PW"
echo ""

RESPONSE=$(curl -s -X POST http://localhost:8000/v1/auth/login \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"admin@localhost\",\"password\":\"$PW\"}")

if echo "$RESPONSE" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if 'access_token' in d else 1)" 2>/dev/null; then
  echo -e "  ${GREEN}✓ LOGIN SUCCESSFUL!${RESET}"
  echo ""
  echo "  The app is ready. Open your browser to:"
  echo "  http://localhost:3000"
else
  echo -e "  ${RED}✗ Login failed${RESET}"
  echo "  Response: $RESPONSE"
fi

echo ""
read -p "  Press ENTER to close..."
