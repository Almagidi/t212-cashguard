#!/bin/bash
set -u
set -o pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
GREEN='\033[0;32m'; RED='\033[0;31m'; YELLOW='\033[1;33m'; BOLD='\033[1m'; RESET='\033[0m'

API_URL="${API_URL:-http://localhost:8000}"
WEB_URL="${WEB_URL:-http://localhost:3000}"

if [ -f "$PROJECT_ROOT/.env" ]; then
  set -a
  # shellcheck disable=SC1090
  source "$PROJECT_ROOT/.env"
  set +a
else
  echo -e "${RED}  ✗ .env not found${RESET}"
  echo "  Run launcher/1. Setup (Run First).command, or create .env before verifying login."
  echo ""
  read -p "  Press ENTER to close..."
  exit 1
fi

EMAIL="${ADMIN_EMAIL:-admin@localhost}"
PW="${ADMIN_PASSWORD:-}"

echo ""
echo -e "${BOLD}  CashGuard — Login Verification${RESET}"
echo "  ─────────────────────────────────────────"
echo "  Normal API: $API_URL"
echo "  Normal Web: $WEB_URL"
echo "  Testing: $EMAIL / $PW"
echo ""

if [ -z "$PW" ]; then
  echo -e "  ${RED}✗ ADMIN_PASSWORD is empty in .env${RESET}"
  echo "  Set ADMIN_PASSWORD in .env, then run this verifier again."
  echo ""
  read -p "  Press ENTER to close..."
  exit 1
fi

if ! curl -sf "$API_URL/v1/health/live" >/dev/null 2>&1; then
  echo -e "  ${RED}✗ Normal API is not reachable at $API_URL/v1/health/live${RESET}"
  echo "  Start the normal app with launcher/2. Start CashGuard.command."
  echo "  Manual QA uses http://127.0.0.1:8002 and is intentionally separate."
  echo ""
  read -p "  Press ENTER to close..."
  exit 1
fi

HTTP_FILE="$(mktemp)"
RESPONSE=$(curl -sS -o "$HTTP_FILE" -w "%{http_code}" -X POST "$API_URL/v1/auth/login" \
  -H "Content-Type: application/json" \
  -d "{\"email\":\"$EMAIL\",\"password\":\"$PW\"}" 2>/tmp/cashguard-login-verify.err || true)
BODY="$(cat "$HTTP_FILE" 2>/dev/null || true)"
rm -f "$HTTP_FILE"

if [ "$RESPONSE" = "200" ] && echo "$BODY" | python3 -c "import sys,json; d=json.load(sys.stdin); exit(0 if 'access_token' in d else 1)" 2>/dev/null; then
  echo -e "  ${GREEN}✓ LOGIN SUCCESSFUL!${RESET}"
  echo ""
  echo "  The app is ready. Open your browser to:"
  echo "  $WEB_URL"
else
  echo -e "  ${RED}✗ Login failed${RESET}"
  echo "  HTTP status: ${RESPONSE:-curl failed}"
  if [ -s /tmp/cashguard-login-verify.err ]; then
    echo "  Curl error:"
    sed 's/^/    /' /tmp/cashguard-login-verify.err
  fi
  echo "  Response body:"
  echo "${BODY:-"(empty)"}" | sed 's/^/    /'
  echo ""
  echo "  Useful checks:"
  echo "    launcher/5. Check Status.command"
  echo "    curl -i $API_URL/v1/health/deps"
  echo "    tail -40 $PROJECT_ROOT/logs/api.log"
fi

rm -f /tmp/cashguard-login-verify.err
echo ""
read -p "  Press ENTER to close..."
