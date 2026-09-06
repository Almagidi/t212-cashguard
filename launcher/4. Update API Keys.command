#!/bin/bash
# Broker credentials must enter through the authenticated application so they
# are encrypted before storage. This launcher intentionally performs no writes.

set -u

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

echo ""
echo "  CashGuard credential update"
echo ""
echo "  This launcher does not collect or store API credentials."
echo "  Start CashGuard, sign in, and use the Broker page to configure a"
echo "  Trading 212 demo connection. The application encrypts credentials"
echo "  before database storage."
echo ""
echo "  Runtime mode is configured separately. Live trading remains prohibited."
echo ""
read -p "  Press ENTER to close..."
