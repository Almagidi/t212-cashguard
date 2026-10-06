#!/bin/bash
# ============================================================
# T212 CashGuard Trader — First-Time Setup Wizard
# Double-click this file to run it.
# ============================================================

# Make Terminal stay open on Mac
if [ "$TERM_PROGRAM" != "iTerm.app" ] && [ -z "$LAUNCHED_FROM_DOCK" ]; then
    export LAUNCHED_FROM_DOCK=1
fi

# ── Colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

clear
echo ""
echo -e "${BOLD}${BLUE}╔══════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${BLUE}║         T212 CashGuard Trader — First-Time Setup             ║${RESET}"
echo -e "${BOLD}${BLUE}╚══════════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  This wizard will:"
echo "  1. Install all required tools (Homebrew, Python, Node, Docker)"
echo "  2. Create a protected mock-only local configuration"
echo "  3. Set everything up automatically"
echo "  4. Launch the app in your browser"
echo ""
echo "  Total time: 5-15 minutes (depending on download speed)"
echo ""
read -p "  Press ENTER to begin, or Ctrl+C to cancel..."
echo ""

# ── Navigate to project root ──────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$PROJECT_ROOT"

step() { echo -e "\n${BOLD}${CYAN}▸ $1${RESET}"; }
ok()   { echo -e "  ${GREEN}✓${RESET} $1"; }
warn() { echo -e "  ${YELLOW}⚠${RESET}  $1"; }
fail() { echo -e "  ${RED}✗${RESET} $1"; }
info() { echo -e "  ${BLUE}ℹ${RESET}  $1"; }

# ── Step 1: Check/Install Homebrew ───────────────────────────────────────────
step "Checking Homebrew..."
if command -v brew &>/dev/null; then
    ok "Homebrew already installed"
else
    warn "Homebrew not found — installing now..."
    echo "  (This may ask for your Mac password)"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to path for Apple Silicon
    if [ -f "/opt/homebrew/bin/brew" ]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
        echo 'eval "$(/opt/homebrew/bin/brew shellenv)"' >> ~/.zprofile
    fi
    ok "Homebrew installed"
fi

# ── Step 2: Check/Install Python 3.11+ ───────────────────────────────────────
step "Checking Python..."
PYTHON=""
for cmd in python3.12 python3.11 python3; do
    if command -v $cmd &>/dev/null; then
        ver=$($cmd -c "import sys; print(sys.version_info.minor)" 2>/dev/null)
        maj=$($cmd -c "import sys; print(sys.version_info.major)" 2>/dev/null)
        if [ "$maj" = "3" ] && [ "$ver" -ge "11" ] 2>/dev/null; then
            PYTHON=$cmd
            break
        fi
    fi
done

if [ -n "$PYTHON" ]; then
    ok "Python $($PYTHON --version) found at $(which $PYTHON)"
else
    warn "Python 3.11+ not found — installing..."
    brew install python@3.12
    PYTHON=$(brew --prefix)/bin/python3.12
    ok "Python installed: $($PYTHON --version)"
fi

PIP="$PYTHON -m pip"

# ── Step 3: Check/Install Node.js 20+ ────────────────────────────────────────
step "Checking Node.js..."
if command -v node &>/dev/null; then
    NODE_VER=$(node -e "console.log(process.version.split('.')[0].replace('v',''))")
    if [ "$NODE_VER" -ge "20" ] 2>/dev/null; then
        ok "Node.js $(node --version) found"
    else
        warn "Node.js version too old (need 20+) — upgrading..."
        brew install node@20
        brew link node@20 --force --overwrite 2>/dev/null || true
        ok "Node.js $(node --version) installed"
    fi
else
    warn "Node.js not found — installing..."
    brew install node@20
    brew link node@20 --force --overwrite 2>/dev/null || true
    ok "Node.js $(node --version) installed"
fi

NPM=$(which npm)

# ── Step 4: Check/Start Docker ───────────────────────────────────────────────
step "Checking Docker Desktop..."
if ! command -v docker &>/dev/null; then
    echo ""
    warn "Docker Desktop is NOT installed."
    echo ""
    echo "  Docker is required. Please:"
    echo "  1. Go to: https://www.docker.com/products/docker-desktop/"
    echo "  2. Download the Apple Silicon version"
    echo "  3. Install it, then re-run this script"
    echo ""
    echo "  Opening Docker Desktop download page..."
    open "https://www.docker.com/products/docker-desktop/"
    echo ""
    read -p "  Once Docker is installed and running, press ENTER to continue..."
fi

# Start Docker if not running
if ! docker info &>/dev/null 2>&1; then
    warn "Docker is not running — starting it..."
    open -a Docker
    echo "  Waiting for Docker to start (up to 60 seconds)..."
    for i in $(seq 1 60); do
        sleep 2
        if docker info &>/dev/null 2>&1; then
            ok "Docker is running"
            break
        fi
        if [ "$i" = "30" ]; then
            echo "  Still waiting..."
        fi
        if [ "$i" = "60" ]; then
            fail "Docker didn't start in time"
            echo "  Please open Docker Desktop manually and run this script again"
            exit 1
        fi
    done
else
    ok "Docker is running"
fi

# ── Step 5: Create mock-only configuration ───────────────────────────────────
step "Setting up your configuration..."
echo ""

if [ -f "$PROJECT_ROOT/.env" ]; then
    ok ".env already exists — preserving it"
    info "Setup never changes runtime mode or broker credentials"
else
    echo "  CashGuard starts in mock mode and does not ask for broker credentials."
    echo "  Demo broker credentials can be added later in the Broker page, where"
    echo "  they are encrypted before database storage. Live trading is prohibited."
    echo ""
    read -s -p "  Admin password for the dashboard (at least 8 characters): " ADMIN_PASS
    echo ""
    while [ ${#ADMIN_PASS} -lt 8 ]; do
        echo "  Password must be at least 8 characters"
        read -s -p "  Admin password: " ADMIN_PASS
        echo ""
    done

    SECRET_KEY=$(openssl rand -hex 32)
    MASTER_KEY=$(openssl rand -hex 32)

    umask 077
    cat > "$PROJECT_ROOT/.env" << ENVEOF
# Generated by CashGuard setup wizard
APP_MODE=mock
SECRET_KEY=${SECRET_KEY}
MASTER_KEY=${MASTER_KEY}

POSTGRES_USER=cashguard
POSTGRES_PASSWORD=cashguard_secret
POSTGRES_DB=cashguard
DATABASE_URL=postgresql+asyncpg://cashguard:cashguard_secret@localhost:5432/cashguard

REDIS_PASSWORD=cashguard_redis
REDIS_URL=redis://:cashguard_redis@localhost:6379/0

ADMIN_EMAIL=admin@localhost
ADMIN_PASSWORD=${ADMIN_PASS:-changeme123}

T212_ENVIRONMENT=demo
MARKET_DATA_PROVIDER=mock
LIVE_TRADING_ENABLED=false

COOKIE_SECURE=false
COOKIE_SAMESITE=lax
CORS_ORIGINS=http://localhost:3000,http://127.0.0.1:3000

NEXT_PUBLIC_API_URL=http://localhost:8000
NEXT_PUBLIC_APP_MODE=mock

LOG_LEVEL=INFO
DEBUG=false
ENVEOF

    chmod 600 "$PROJECT_ROOT/.env"
    ok ".env file created in protected mock-only mode"
fi

# ── Step 6: Install Python dependencies ──────────────────────────────────────
step "Setting up Python virtual environment..."
cd "$PROJECT_ROOT"

# Create virtual environment inside the project folder
VENV_DIR="$PROJECT_ROOT/venv"
if [ ! -f "$VENV_DIR/bin/python" ]; then
    "$PYTHON" -m venv "$VENV_DIR"
    ok "Virtual environment created at $VENV_DIR"
else
    ok "Virtual environment already exists"
fi

PYTHON="$VENV_DIR/bin/python"
PIP="$VENV_DIR/bin/pip"

step "Installing Python dependencies (this takes 2-3 minutes)..."
cd "$PROJECT_ROOT/apps/api"
$PIP install --upgrade pip -q
$PIP install -r requirements.txt -q 2>&1 | tail -3
ok "Python packages installed"

# ── Step 7: Install Node dependencies ────────────────────────────────────────
step "Installing Node.js dependencies..."
cd "$PROJECT_ROOT/apps/web"
$NPM install --silent 2>&1 | tail -3
ok "Node packages installed"
cd "$PROJECT_ROOT"

# ── Step 8: Start database ────────────────────────────────────────────────────
step "Starting database services..."
cd "$PROJECT_ROOT"
docker-compose up -d postgres redis

echo "  Waiting for database to be ready..."
for i in $(seq 1 30); do
    sleep 2
    if docker-compose exec -T postgres pg_isready -U cashguard &>/dev/null 2>&1; then
        ok "Database ready"
        break
    fi
    if [ "$i" = "30" ]; then
        fail "Database didn't start — try running setup again"
        exit 1
    fi
done

# ── Step 9: Run migrations and seed ──────────────────────────────────────────
step "Setting up database schema..."
cd "$PROJECT_ROOT/apps/api"

source "$PROJECT_ROOT/.env" 2>/dev/null || true
export DATABASE_URL="postgresql+asyncpg://cashguard:cashguard_secret@localhost:5432/cashguard"

$PYTHON -m alembic upgrade head 2>&1 | tail -5
ok "Database tables created"

$PYTHON -m app.db.seed 2>&1 | tail -3
ok "Database seeded with defaults"

# ── Step 10: Mark setup complete ─────────────────────────────────────────────
touch "$PROJECT_ROOT/.setup_complete"

# ── Done ─────────────────────────────────────────────────────────────────────
clear
echo ""
echo -e "${BOLD}${GREEN}╔══════════════════════════════════════════════════════════════╗${RESET}"
echo -e "${BOLD}${GREEN}║              Setup Complete! Ready to launch.                ║${RESET}"
echo -e "${BOLD}${GREEN}╚══════════════════════════════════════════════════════════════╝${RESET}"
echo ""
echo "  What to do next:"
echo ""
echo "  1. Close this window"
echo -e "  2. Double-click ${BOLD}launcher/2. Start CashGuard.command${RESET} to launch the normal app"
echo "  3. Your browser will open automatically"
echo ""
echo "  Login details:"
echo "  URL:      http://localhost:3000"
echo "  Email:    admin@localhost"
echo "  Password: the value you entered during setup"

echo ""
read -p "  Press ENTER to close..."
