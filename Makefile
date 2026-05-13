
.PHONY: help setup dev up down migrate seed reset test lint typecheck e2e e2e-operator e2e-operator-integration readiness-full logs clean launcher-check normal-status stop-normal-ports demo-mock demo-mock-stop operator-manual operator-manual-stop operator-manual-check manual-status stop-manual-ports

.PHONY: help setup dev up down migrate seed reset test lint typecheck e2e e2e-operator e2e-operator-integration readiness-full logs clean operator-manual operator-manual-stop

.PHONY: help setup dev up down migrate seed reset test lint typecheck e2e e2e-operator logs clean

SHELL := /bin/bash
PYTHON ?= python3.12
.DEFAULT_GOAL := help

GREEN  := $(shell printf '\033[0;32m')
YELLOW := $(shell printf '\033[1;33m')
RED    := $(shell printf '\033[0;31m')
RESET  := $(shell printf '\033[0m')
NORMAL_API_PORT ?= 8000
NORMAL_WEB_PORT ?= 3000

help: ## Show this help message
	@echo ""
	@echo "  T212 CashGuard Trader — Local Development Commands"
	@echo ""
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(RESET) %s\n", $$1, $$2}'
	@echo ""

setup: ## First-time setup: copy .env, install deps, build docker, migrate, seed
	@echo "$(YELLOW)→ Setting up T212 CashGuard Trader...$(RESET)"
	@if [ ! -f .env ]; then cp .env.example .env; echo "$(GREEN)✓ .env created from .env.example$(RESET)"; else echo "  .env already exists, skipping"; fi
	@echo "$(YELLOW)→ Installing backend dependencies...$(RESET)"
	cd apps/api && pip install -r requirements.txt -q
	@echo "$(YELLOW)→ Installing frontend dependencies...$(RESET)"
	cd apps/web && npm install --silent
	@echo "$(GREEN)✓ Dependencies installed$(RESET)"
	@echo ""
	@echo "$(YELLOW)→ Starting infrastructure (postgres + redis)...$(RESET)"
	docker-compose up -d postgres redis
	@sleep 5
	$(MAKE) migrate
	$(MAKE) seed
	@echo ""
	@echo "$(GREEN)✓ Setup complete! Run 'make dev' to start the development servers.$(RESET)"

dev: ## Start backend and frontend in development mode (requires infra to be running)
	@echo "$(YELLOW)→ Starting development servers...$(RESET)"
	@trap 'kill %1 %2 2>/dev/null; exit' INT; \
	cd apps/api && uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload & \
	cd apps/web && npm run dev & \
	wait

up: ## Start the full Docker Compose stack
	@echo "$(YELLOW)→ Starting full Docker stack...$(RESET)"
	docker-compose up -d
	@echo "$(GREEN)✓ Stack running$(RESET)"
	@echo "  Frontend: http://localhost:3000"
	@echo "  Backend:  http://localhost:8000"
	@echo "  API docs: http://localhost:8000/docs"

down: ## Stop the Docker Compose stack
	@echo "$(YELLOW)→ Stopping Docker stack...$(RESET)"
	docker-compose down
	@echo "$(GREEN)✓ Stack stopped$(RESET)"

migrate: ## Run database migrations
	@echo "$(YELLOW)→ Running database migrations...$(RESET)"
	cd apps/api && PYTHONPATH=. alembic upgrade head
	@echo "$(GREEN)✓ Migrations complete$(RESET)"

seed: ## Seed database with demo data
	@echo "$(YELLOW)→ Seeding database...$(RESET)"
	@if [ "$$(docker-compose ps -q api)" ] && [ "$$(docker inspect -f '{{.State.Running}}' t212_api 2>/dev/null)" = "true" ]; then \
		docker-compose exec -T api python -m app.db.seed; \
	else \
		cd apps/api && PYTHONPATH=. python -m app.db.seed; \
	fi
	@echo "$(GREEN)✓ Database seeded$(RESET)"

reset: ## Reset database (drop + recreate + migrate + seed)
	@echo "$(RED)⚠ This will destroy all local data. Press Ctrl+C to abort...$(RESET)"
	@sleep 3
	@echo "$(YELLOW)→ Resetting database...$(RESET)"
	cd apps/api && alembic downgrade base
	$(MAKE) migrate
	$(MAKE) seed
	@echo "$(GREEN)✓ Database reset complete$(RESET)"

test: ## Run all tests (backend + frontend)
	@echo "$(YELLOW)→ Running backend tests...$(RESET)"
	cd apps/api && python -m pytest tests/ -v --tb=short
	@echo "$(YELLOW)→ Running frontend tests...$(RESET)"
	cd apps/web && npm run test -- --watchAll=false
	@echo "$(GREEN)✓ All tests complete$(RESET)"

test-backend: ## Run backend tests only
	cd apps/api && python -m pytest tests/ -v --tb=short --cov=app --cov-report=term-missing

test-frontend: ## Run frontend tests only
	cd apps/web && npm run test -- --watchAll=false --coverage

lint: ## Run linters (ruff for backend, eslint for frontend)
	@echo "$(YELLOW)→ Linting backend...$(RESET)"
	cd apps/api && python -m ruff check app/ tests/
	@echo "$(YELLOW)→ Linting frontend...$(RESET)"
	cd apps/web && npm run lint
	@echo "$(GREEN)✓ Lint complete$(RESET)"

typecheck: ## Run type checkers (mypy for backend, tsc for frontend)
	@echo "$(YELLOW)→ Type-checking backend...$(RESET)"
	cd apps/api && python -m mypy app/ --ignore-missing-imports
	@echo "$(YELLOW)→ Type-checking frontend...$(RESET)"
	cd apps/web && npm run typecheck
	@echo "$(GREEN)✓ Type-check complete$(RESET)"

format: ## Auto-format all code
	cd apps/api && python -m ruff format app/ tests/ && python -m ruff check app/ tests/ --fix
	cd apps/web && npm run format

e2e: ## Run Playwright end-to-end tests
	@echo "$(YELLOW)→ Running e2e tests...$(RESET)"
	cd apps/web && npx playwright test
	@echo "$(GREEN)✓ E2E tests complete$(RESET)"

e2e-operator: ## Run mock-mode operator dashboard readiness e2e test
	@echo "$(YELLOW)→ Running operator dashboard readiness e2e test...$(RESET)"
	cd apps/web && E2E_MOCK_API=1 E2E_WEB_PORT=3100 BASE_URL=http://localhost:3100 NEXT_PUBLIC_APP_MODE=mock NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 npx playwright test tests/e2e/operator.spec.ts
	@echo "$(GREEN)✓ Operator dashboard readiness e2e complete$(RESET)"

logs: ## Tail all Docker logs
	docker-compose logs -f

logs-api: ## Tail API logs
	docker-compose logs -f api

logs-worker: ## Tail worker logs
	docker-compose logs -f worker

clean: ## Remove build artifacts and caches
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .pytest_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .mypy_cache -exec rm -rf {} + 2>/dev/null || true
	find . -type d -name .ruff_cache -exec rm -rf {} + 2>/dev/null || true
	cd apps/web && rm -rf .next/ out/ coverage/ 2>/dev/null || true
	@echo "$(GREEN)✓ Clean complete$(RESET)"

check-all: lint typecheck test ## Run lint, typecheck, and tests in sequence
	@echo "$(GREEN)✓ All checks passed$(RESET)"

.PHONY: smoke readiness paper-check e2e-operator-integration readiness-full

smoke:
	cd apps/api && DATABASE_URL=sqlite+aiosqlite:///:memory: REDIS_URL=redis://localhost:6379/15 SECRET_KEY=test-secret-key-32-chars-minimum-x MASTER_KEY=test-master-key-32-chars-minimum-x APP_MODE=mock python3.12 -m pytest tests/smoke/ -v --tb=short --no-cov

readiness: smoke e2e-operator

paper-check: ## Run targeted paper execution backend safety tests
	cd apps/api && DATABASE_URL=sqlite+aiosqlite:///:memory: REDIS_URL=redis://localhost:6379/15 SECRET_KEY=test-secret-key-32-chars-minimum-x MASTER_KEY=test-master-key-32-chars-minimum-x APP_MODE=mock python3.12 -m pytest tests/integration/test_paper_execution.py tests/unit/test_operator_status_api.py -q --no-cov

e2e-operator-integration: ## Run real-backend integration e2e for operator dashboard (SQLite, APP_MODE=mock, ports 8001/3001)
	@if lsof -tiTCP:8001 -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "$(RED)Integration API port 8001 is already in use.$(RESET)"; \
		lsof -nP -iTCP:8001 -sTCP:LISTEN 2>/dev/null | sed 's/^/  /'; \
		echo "Run make manual-status to inspect, or make stop-manual-ports if it is stale and project-owned."; \
		exit 1; \
	fi
	@if lsof -tiTCP:3001 -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "$(RED)Integration web port 3001 is already in use.$(RESET)"; \
		lsof -nP -iTCP:3001 -sTCP:LISTEN 2>/dev/null | sed 's/^/  /'; \
		echo "Run make manual-status to inspect, or make stop-manual-ports if it is stale and project-owned."; \
		exit 1; \
	fi
	@echo "$(YELLOW)→ Initialising SQLite integration DB...$(RESET)"
	@cd apps/api && \
		INTEGRATION_DB_PATH=/tmp/t212_integration_test.db \
		DATABASE_URL="sqlite+aiosqlite:////tmp/t212_integration_test.db" \
		REDIS_URL=redis://localhost:6379/15 \
		SECRET_KEY=integration-test-secret-key-32-chars-x \
		MASTER_KEY=integration-test-master-key-32-chars-x \
		APP_MODE=mock \
		ADMIN_EMAIL=admin@localhost \
		ADMIN_PASSWORD=change-me \
		PYTHONPATH=. python3.12 scripts/init_integration_db.py
	@echo "$(YELLOW)→ Starting API on :8001 (background)...$(RESET)"
	@set -e; \
	cd apps/api; \
	DATABASE_URL="sqlite+aiosqlite:////tmp/t212_integration_test.db" \
		REDIS_URL=redis://localhost:6379/15 \
		SECRET_KEY=integration-test-secret-key-32-chars-x \
		MASTER_KEY=integration-test-master-key-32-chars-x \
		APP_MODE=mock \
		ADMIN_EMAIL=admin@localhost \
		ADMIN_PASSWORD=change-me \
		CORS_ORIGINS="http://localhost:3001,http://127.0.0.1:3001" \
		PYTHONPATH=. \
		uvicorn app.main:app --host 127.0.0.1 --port 8001 --no-access-log & \
	API_PID=$$!; \
	cleanup() { kill $$API_PID 2>/dev/null || true; wait $$API_PID 2>/dev/null || true; }; \
	trap cleanup EXIT INT TERM; \
	echo "$(YELLOW)  → Waiting for API to be ready...$(RESET)"; \
	API_READY=0; \
	for i in $$(seq 1 20); do \
		if ! kill -0 $$API_PID 2>/dev/null; then \
			echo "$(RED)Integration API exited before readiness.$(RESET)"; \
			exit 1; \
		fi; \
		if curl -sf http://127.0.0.1:8001/v1/health/live >/dev/null 2>&1; then \
			API_READY=1; \
			break; \
		fi; \
		sleep 1; \
	done; \
	if [ "$$API_READY" != "1" ]; then \
		echo "$(RED)Integration API did not become ready on port 8001.$(RESET)"; \
		exit 1; \
	fi; \
	echo "$(YELLOW)  → Running Playwright integration tests...$(RESET)"; \
	cd $(CURDIR)/apps/web && \
		NEXT_PUBLIC_API_URL=http://127.0.0.1:8001 \
		NEXT_PUBLIC_APP_MODE=mock \
		BASE_URL=http://localhost:3001 \
		ADMIN_EMAIL=admin@localhost \
		ADMIN_PASSWORD=change-me \
		INTEGRATION_API_PORT=8001 \
		INTEGRATION_WEB_PORT=3001 \
		npx playwright test --config=playwright.integration.config.ts; \
	echo "$(GREEN)✓ Operator integration e2e complete$(RESET)"

readiness-full: smoke e2e-operator e2e-operator-integration ## Full readiness: smoke + mock e2e + integration e2e

# ─── Normal launcher — ports 8000/3000, mode from .env ───────────────────────
launcher-check: normal-status manual-status ## Show normal and manual launcher port/PID status without stopping anything

normal-status: ## Show normal launcher status on ports 8000/3000 without stopping anything
	@echo "$(YELLOW)→ Normal launcher status$(RESET)"
	@echo "  Flow: normal app startup"
	@echo "  API : http://localhost:$(NORMAL_API_PORT)"
	@echo "  Web : http://localhost:$(NORMAL_WEB_PORT)"
	@if [ -f .env ]; then \
		mode=$$(grep -E '^APP_MODE=' .env | tail -1 | cut -d= -f2-); \
		echo "  APP_MODE from .env: $${mode:-mock}"; \
	else \
		echo "  APP_MODE from .env: .env missing, launcher default is mock"; \
	fi
	@echo ""
	@echo "  Saved normal launcher PIDs:"
	@if [ -f .launcher.pid ]; then \
		launcher=$$(cat .launcher.pid 2>/dev/null || true); \
		if [ -n "$$launcher" ] && kill -0 "$$launcher" 2>/dev/null; then \
			echo "    Launcher PID $$launcher running"; \
		else \
			echo "    Launcher PID $${launcher:-unknown} not running"; \
		fi; \
	else \
		echo "    No .launcher.pid file found"; \
	fi
	@if [ -f .pids ]; then \
		read api web celery < .pids || true; \
		for pair in "API:$$api" "Web:$$web" "Workers:$$celery"; do \
			label=$${pair%%:*}; pid=$${pair#*:}; \
			if [ -n "$$pid" ] && kill -0 "$$pid" 2>/dev/null; then \
				echo "    $$label PID $$pid running"; \
			else \
				echo "    $$label PID $${pid:-unknown} not running"; \
			fi; \
		done; \
	else \
		echo "    No .pids file found"; \
	fi
	@echo ""
	@for port in $(NORMAL_API_PORT) $(NORMAL_WEB_PORT); do \
		echo "  Port $$port listener:"; \
		if lsof -nP -iTCP:$$port -sTCP:LISTEN >/dev/null 2>&1; then \
			lsof -nP -iTCP:$$port -sTCP:LISTEN 2>/dev/null | sed 's/^/    /'; \
		else \
			echo "    free"; \
		fi; \
	done
	@echo ""
	@if curl -sf http://localhost:$(NORMAL_API_PORT)/v1/health/live >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ Normal API health responds$(RESET)"; \
	else \
		echo "  $(YELLOW)⚠ Normal API health is not reachable$(RESET)"; \
	fi
	@if curl -sf http://localhost:$(NORMAL_WEB_PORT)/auth/login >/dev/null 2>&1; then \
		echo "  $(GREEN)✓ Normal web login page responds$(RESET)"; \
	else \
		echo "  $(YELLOW)⚠ Normal web login page is not reachable$(RESET)"; \
	fi

stop-normal-ports: ## Stop only project-owned stale listeners on normal ports 8000/3000
	@echo "$(YELLOW)→ Stopping project-owned normal-port listeners only: $(NORMAL_API_PORT) $(NORMAL_WEB_PORT)$(RESET)"
	@for port in $(NORMAL_API_PORT) $(NORMAL_WEB_PORT); do \
		pids=$$(lsof -tiTCP:$$port -sTCP:LISTEN 2>/dev/null || true); \
		if [ -z "$$pids" ]; then \
			echo "  Port $$port: free"; \
			continue; \
		fi; \
		echo "  Port $$port:"; \
		lsof -nP -iTCP:$$port -sTCP:LISTEN 2>/dev/null | sed 's/^/    /' || true; \
		for pid in $$pids; do \
			cwd=$$(lsof -a -p $$pid -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1); \
			cmd=$$(ps -p $$pid -o command= 2>/dev/null || true); \
			case "$$cwd" in \
				$(CURDIR)|$(CURDIR)/*) \
					echo "    stopping PID $$pid from this repo: $$cmd"; \
					kill $$pid 2>/dev/null || true; \
					;; \
				*) \
					echo "    leaving PID $$pid alone; cwd is $${cwd:-unknown}, not $(CURDIR)"; \
					;; \
			esac; \
		done; \
	done
	@sleep 1
	@for port in $(NORMAL_API_PORT) $(NORMAL_WEB_PORT); do \
		remaining=$$(lsof -tiTCP:$$port -sTCP:LISTEN 2>/dev/null || true); \
		if [ -z "$$remaining" ]; then \
			echo "  $(GREEN)✓ Port $$port is free$(RESET)"; \
		else \
			echo "  $(YELLOW)⚠ Port $$port still has listener(s): $$remaining$(RESET)"; \
			echo "    Inspect with: make normal-status"; \
		fi; \
	done


# ─── Manual QA — local real-backend in mock mode ──────────────────────────────
MANUAL_QA_API_PORT ?= 8002
MANUAL_QA_WEB_PORT ?= 3002
MANUAL_QA_DB_PATH  ?= /tmp/t212_manual_qa.db
MANUAL_QA_API_PID  ?= /tmp/t212_manual_api.pid
MANUAL_QA_WEB_PID  ?= /tmp/t212_manual_web.pid
MANUAL_QA_PORTS    ?= 8001 8002 3001 3002 3100

demo-mock: operator-manual ## Start demo-ready mock stack with seeded local data

demo-mock-stop: operator-manual-stop ## Stop the demo-ready mock stack

operator-manual: ## Start local manual QA servers (API :8002, web :3002, APP_MODE=mock, no broker creds needed)
	@set -e; \
		for spec in "API:$(MANUAL_QA_API_PORT):$(MANUAL_QA_API_PID)" "Web:$(MANUAL_QA_WEB_PORT):$(MANUAL_QA_WEB_PID)"; do \
			name=$${spec%%:*}; rest=$${spec#*:}; port=$${rest%%:*}; pidfile=$${rest#*:}; \
			if [ -f "$$pidfile" ]; then \
				pid=$$(cat "$$pidfile" 2>/dev/null || true); \
				if [ -n "$$pid" ] && kill -0 "$$pid" 2>/dev/null; then \
					cwd=$$(lsof -a -p "$$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1); \
					case "$$cwd" in \
						$(CURDIR)|$(CURDIR)/*) \
							echo "$(RED)$$name appears to already be running from this repo via PID $$pid.$(RESET)"; \
							ps -p "$$pid" -o pid=,command= 2>/dev/null | sed 's/^/    /' || true; \
							echo "Run make operator-manual-stop first."; \
							exit 1; \
							;; \
						*) \
							echo "  Removing stale $$name PID file: $$pidfile pointed at PID $$pid outside this repo"; \
							rm -f "$$pidfile"; \
							;; \
					esac; \
				else \
					echo "  Removing stale $$name PID file: $$pidfile"; \
					rm -f "$$pidfile"; \
				fi; \
			fi; \
			pids=$$(lsof -tiTCP:$$port -sTCP:LISTEN 2>/dev/null || true); \
			if [ -n "$$pids" ]; then \
				echo "$(RED)$$name port $$port already has a LISTENING process.$(RESET)"; \
				lsof -nP -iTCP:$$port -sTCP:LISTEN 2>/dev/null | sed 's/^/    /' || true; \
				echo "Run make manual-status to inspect, or choose MANUAL_QA_$${name}_PORT=<port>."; \
				exit 1; \
			fi; \
		done
	@echo "$(YELLOW)→ Initialising manual QA SQLite DB...$(RESET)"
	@cd apps/api && \
		INTEGRATION_DB_PATH=$(MANUAL_QA_DB_PATH) \
		DATABASE_URL="sqlite+aiosqlite:///$(MANUAL_QA_DB_PATH)" \
		REDIS_URL=redis://localhost:6379/15 \
		SECRET_KEY=manual-qa-secret-key-32-chars-xxxxx \
		MASTER_KEY=manual-qa-master-key-32-chars-xxxxx \
		APP_MODE=mock \
		ADMIN_EMAIL=admin@localhost \
		ADMIN_PASSWORD=change-me \
		PYTHONPATH=. python3.12 scripts/init_integration_db.py
	@rm -f /tmp/t212_manual_api.log /tmp/t212_manual_web.log
	@echo "$(YELLOW)→ Starting API on :$(MANUAL_QA_API_PORT) (APP_MODE=mock, background)...$(RESET)"
	@nohup bash -c 'cd apps/api && exec env \
		DATABASE_URL="sqlite+aiosqlite:///$(MANUAL_QA_DB_PATH)" \
		REDIS_URL=redis://localhost:6379/15 \
		SECRET_KEY=manual-qa-secret-key-32-chars-xxxxx \
		MASTER_KEY=manual-qa-master-key-32-chars-xxxxx \
		APP_MODE=mock \
		ADMIN_EMAIL=admin@localhost \
		ADMIN_PASSWORD=change-me \
		CORS_ORIGINS="http://localhost:$(MANUAL_QA_WEB_PORT),http://127.0.0.1:$(MANUAL_QA_WEB_PORT)" \
		PYTHONPATH=. \
		uvicorn app.main:app --host 127.0.0.1 --port $(MANUAL_QA_API_PORT) --no-access-log \
	' >> /tmp/t212_manual_api.log 2>&1 & echo $$! > $(MANUAL_QA_API_PID)
	@echo "$(YELLOW)  → Waiting for API to be ready on :$(MANUAL_QA_API_PORT)...$(RESET)"
	@API_READY=0; \
	for i in $$(seq 1 30); do \
		pid=$$(cat $(MANUAL_QA_API_PID) 2>/dev/null || true); \
		if [ -z "$$pid" ] || ! kill -0 "$$pid" 2>/dev/null; then \
			echo "$(RED)API process exited before becoming ready. See /tmp/t212_manual_api.log.$(RESET)"; \
			tail -80 /tmp/t212_manual_api.log 2>/dev/null || true; \
			exit 1; \
		fi; \
		if curl -sf http://127.0.0.1:$(MANUAL_QA_API_PORT)/v1/health/live >/dev/null 2>&1; then \
			API_READY=1; \
			break; \
		fi; \
		sleep 1; \
	done; \
	if [ "$$API_READY" != "1" ]; then \
		echo "$(RED)API did not become ready on port $(MANUAL_QA_API_PORT). See /tmp/t212_manual_api.log.$(RESET)"; \
		tail -80 /tmp/t212_manual_api.log 2>/dev/null || true; \
		exit 1; \
	fi
	@echo "$(YELLOW)→ Starting web app on :$(MANUAL_QA_WEB_PORT) (background)...$(RESET)"
	@nohup bash -c 'cd apps/web && exec env \
		NEXT_PUBLIC_API_URL=http://127.0.0.1:$(MANUAL_QA_API_PORT) \
		NEXT_PUBLIC_APP_MODE=mock \
		npx next dev -p $(MANUAL_QA_WEB_PORT) \
	' >> /tmp/t212_manual_web.log 2>&1 & echo $$! > $(MANUAL_QA_WEB_PID)
	@WEB_READY=0; \
	for i in $$(seq 1 45); do \
		pid=$$(cat $(MANUAL_QA_WEB_PID) 2>/dev/null || true); \
		if [ -z "$$pid" ] || ! kill -0 "$$pid" 2>/dev/null; then \
			echo "$(RED)Web process exited early. See /tmp/t212_manual_web.log.$(RESET)"; \
			tail -80 /tmp/t212_manual_web.log 2>/dev/null || true; \
			exit 1; \
		fi; \
		if curl -sf http://127.0.0.1:$(MANUAL_QA_WEB_PORT)/auth/login >/dev/null 2>&1; then \
			WEB_READY=1; \
			break; \
		fi; \
		sleep 1; \
	done; \
	if [ "$$WEB_READY" != "1" ]; then \
		echo "$(RED)Web app did not become ready on port $(MANUAL_QA_WEB_PORT). See /tmp/t212_manual_web.log.$(RESET)"; \
		tail -80 /tmp/t212_manual_web.log 2>/dev/null || true; \
		exit 1; \
	fi
	@echo ""
	@echo "$(GREEN)╔══════════════════════════════════════════════════════════╗$(RESET)"
	@echo "$(GREEN)║      T212 CashGuard — Manual QA Servers Running          ║$(RESET)"
	@echo "$(GREEN)╚══════════════════════════════════════════════════════════╝$(RESET)"
	@echo ""
	@echo "  Frontend : http://localhost:$(MANUAL_QA_WEB_PORT)"
	@echo "  Backend  : http://127.0.0.1:$(MANUAL_QA_API_PORT)"
	@echo "  API docs : http://127.0.0.1:$(MANUAL_QA_API_PORT)/docs"
	@echo ""
	@echo "  Login credentials"
	@echo "    Email    : admin@localhost"
	@echo "    Password : change-me"
	@echo ""
	@echo "  Page to open : http://localhost:$(MANUAL_QA_WEB_PORT)/app/operator"
	@echo ""
	@echo "  Logs"
	@echo "    API  : tail -f /tmp/t212_manual_api.log"
	@echo "    Web  : tail -f /tmp/t212_manual_web.log"
	@echo ""
	@echo "  $(YELLOW)When finished run: make operator-manual-stop$(RESET)"
	@echo ""
	@echo "  See docs/operator-manual-qa.md for the full QA checklist."
	@echo ""

operator-manual-stop: ## Stop local manual QA servers started by operator-manual
	@echo "$(YELLOW)→ Stopping manual QA servers...$(RESET)"
	@set -e; \
		for spec in "API:$(MANUAL_QA_API_PID):$(MANUAL_QA_API_PORT)" "Web:$(MANUAL_QA_WEB_PID):$(MANUAL_QA_WEB_PORT)"; do \
			name=$${spec%%:*}; rest=$${spec#*:}; pidfile=$${rest%%:*}; port=$${rest#*:}; \
			if [ ! -f "$$pidfile" ]; then \
				echo "  No $$name PID file; not killing unknown process on port $$port."; \
				continue; \
			fi; \
			pid=$$(cat "$$pidfile" 2>/dev/null || true); \
			if [ -z "$$pid" ] || ! kill -0 "$$pid" 2>/dev/null; then \
				echo "  $$name process already gone; removing stale PID file."; \
				rm -f "$$pidfile"; \
				continue; \
			fi; \
			cwd=$$(lsof -a -p "$$pid" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1); \
			case "$$cwd" in \
				$(CURDIR)|$(CURDIR)/*) \
					kill "$$pid" 2>/dev/null || true; \
					sleep 1; \
					if kill -0 "$$pid" 2>/dev/null; then kill -9 "$$pid" 2>/dev/null || true; fi; \
					echo "  $(GREEN)✓ $$name stopped$(RESET)"; \
					;; \
				*) \
					echo "  Leaving $$name PID $$pid alone; cwd is $${cwd:-unknown}, not $(CURDIR)."; \
					;; \
			esac; \
			rm -f "$$pidfile"; \
		done
	@echo "$(GREEN)✓ Manual QA stop routine complete$(RESET)"

manual-status: ## Show listeners on manual/test ports without stopping anything
	@echo "$(YELLOW)→ Manual QA PID files$(RESET)"
	@for file in $(MANUAL_QA_API_PID) $(MANUAL_QA_WEB_PID); do \
		if [ -f "$$file" ]; then \
			pid=$$(cat "$$file" 2>/dev/null || true); \
			if [ -n "$$pid" ] && kill -0 "$$pid" 2>/dev/null; then \
				echo "  $$file -> PID $$pid alive"; \
				ps -p "$$pid" -o pid=,command= 2>/dev/null | sed 's/^/    /' || true; \
			else \
				echo "  $$file -> stale/dead PID"; \
			fi; \
		else \
			echo "  $$file -> missing"; \
		fi; \
	done
	@echo "$(YELLOW)→ Manual/test port listeners$(RESET)"
	@for port in $(MANUAL_QA_PORTS); do \
		echo ""; \
		echo "Port $$port"; \
		if lsof -nP -iTCP:$$port -sTCP:LISTEN >/dev/null 2>&1; then \
			lsof -nP -iTCP:$$port -sTCP:LISTEN 2>/dev/null; \
		else \
			echo "  free"; \
		fi; \
	done

stop-manual-ports: ## Stop project-owned listeners on known manual/test ports only (8001/8002/3001/3002/3100)
	@echo "$(YELLOW)→ Stopping project-owned listeners on manual/test ports only: $(MANUAL_QA_PORTS)$(RESET)"
	@for port in $(MANUAL_QA_PORTS); do \
		pids=$$(lsof -tiTCP:$$port -sTCP:LISTEN 2>/dev/null || true); \
		if [ -z "$$pids" ]; then \
			echo "  Port $$port: free"; \
			continue; \
		fi; \
		echo "  Port $$port:"; \
		lsof -nP -iTCP:$$port -sTCP:LISTEN 2>/dev/null | sed 's/^/    /' || true; \
		for pid in $$pids; do \
			cwd=$$(lsof -a -p $$pid -d cwd -Fn 2>/dev/null | sed -n 's/^n//p' | head -1); \
			cmd=$$(ps -p $$pid -o command= 2>/dev/null || true); \
			case "$$cwd" in \
				$(CURDIR)|$(CURDIR)/*) \
					echo "    stopping PID $$pid from this repo: $$cmd"; \
					kill $$pid 2>/dev/null || true; \
					;; \
				*) \
					echo "    leaving PID $$pid alone; cwd is $${cwd:-unknown}, not $(CURDIR)"; \
					;; \
			esac; \
		done; \
	done
	@echo "$(GREEN)✓ Manual/test port cleanup complete$(RESET)"

operator-manual-check: ## Curl manual QA endpoints with auth against API :8002
	@echo "$(YELLOW)→ Checking manual QA API endpoints on :$(MANUAL_QA_API_PORT)...$(RESET)"
	@set -e; \
		tmp=/tmp/t212_manual_check.json; \
		login_tmp=/tmp/t212_manual_login.json; \
		code=$$(curl -sS -o "$$tmp" -w '%{http_code}' http://127.0.0.1:$(MANUAL_QA_API_PORT)/v1/health/live || true); \
		echo "  /v1/health/live -> $$code"; \
		if [ "$$code" != "200" ]; then \
			echo "$(RED)Manual QA API is not reachable on http://127.0.0.1:$(MANUAL_QA_API_PORT).$(RESET)"; \
			echo "Run make manual-status, then make operator-manual."; \
			exit 1; \
		fi; \
		login_code=$$(curl -sS -o "$$login_tmp" -w '%{http_code}' -X POST http://127.0.0.1:$(MANUAL_QA_API_PORT)/v1/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@localhost","password":"change-me"}' || true); \
		echo "  /v1/auth/login -> $$login_code"; \
		if [ "$$login_code" != "200" ]; then \
			echo "$(RED)Manual QA login failed.$(RESET)"; \
			cat "$$login_tmp" 2>/dev/null || true; \
			exit 1; \
		fi; \
		TOKEN=$$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1])).get("access_token", ""))' "$$login_tmp" 2>/dev/null || true); \
		if [ -z "$$TOKEN" ]; then \
			echo "$(RED)Login response did not include an access_token.$(RESET)"; \
			cat "$$login_tmp" 2>/dev/null || true; \
			exit 1; \
		fi; \
		for endpoint in /v1/auth/me /v1/operator/status /v1/kraken/dca/status /v1/kraken/dca/activity /v1/kraken/dca/configs /v1/account/summary /v1/account/cash-guard /v1/positions; do \
			code=$$(curl -sS -o "$$tmp" -w '%{http_code}' -H "Authorization: Bearer $$TOKEN" http://127.0.0.1:$(MANUAL_QA_API_PORT)$$endpoint || true); \
			echo "  $$endpoint -> $$code"; \
			if [ "$$code" != "200" ]; then \
				echo "$(RED)Endpoint failed: $$endpoint$(RESET)"; \
				cat "$$tmp" 2>/dev/null || true; \
				exit 1; \
			fi; \
		done
	@echo "$(GREEN)✓ Manual QA API endpoints returned 200$(RESET)"
# ─── Local validation baseline ───────────────────────────────────────────────
.PHONY: validate validate-api validate-web validate-e2e

t212-demo-readonly-smoke: ## Run Trading 212 demo read-only smoke test; requires T212_API_KEY/T212_API_SECRET
	@echo "$(YELLOW)→ Running Trading 212 demo read-only smoke test...$(RESET)"
	cd apps/api && \
		APP_MODE=demo \
		T212_ENVIRONMENT=demo \
		LIVE_TRADING_ENABLED=false \
		MARKET_DATA_PROVIDER=mock \
		PYTHONPATH=. \
		$(PYTHON) scripts/t212_demo_readonly_smoke.py
	@echo "$(GREEN)✓ Trading 212 demo read-only smoke passed$(RESET)"

validate: validate-api validate-web validate-e2e e2e-operator-integration ## Run full local validation baseline: API, web, E2E, and operator integration
	@echo "$(GREEN)✓ Full local validation baseline passed$(RESET)"

validate-api: ## Run backend pytest suite with coverage gate
	@echo "$(YELLOW)→ Running backend validation...$(RESET)"
	cd apps/api && $(PYTHON) -m pytest -q
	@echo "$(GREEN)✓ Backend validation passed$(RESET)"

validate-web: ## Run frontend typecheck, lint, unit tests, and production build
	@echo "$(YELLOW)→ Running frontend typecheck...$(RESET)"
	cd apps/web && npm run typecheck
	@echo "$(YELLOW)→ Running frontend lint...$(RESET)"
	cd apps/web && npm run lint
	@echo "$(YELLOW)→ Running frontend unit tests...$(RESET)"
	cd apps/web && npm test
	@echo "$(YELLOW)→ Running frontend production build...$(RESET)"
	cd apps/web && npm run build
	@echo "$(GREEN)✓ Frontend validation passed$(RESET)"

validate-e2e: ## Run local Playwright E2E against mock market-data backend
	@echo "$(YELLOW)→ Checking Docker daemon...$(RESET)"
	@if ! docker info >/dev/null 2>&1; then \
		echo "$(RED)Docker is not running. Start Docker Desktop, then rerun make validate-e2e.$(RESET)"; \
		exit 1; \
	fi
	@echo "$(YELLOW)→ Checking local ports...$(RESET)"
	@if lsof -tiTCP:8000 -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "$(RED)Port 8000 is already in use. Stop the running API first.$(RESET)"; \
		lsof -nP -iTCP:8000 -sTCP:LISTEN 2>/dev/null | sed 's/^/  /'; \
		exit 1; \
	fi
	@if lsof -tiTCP:3000 -sTCP:LISTEN >/dev/null 2>&1; then \
		echo "$(RED)Port 3000 is already in use. Stop the running web server first.$(RESET)"; \
		lsof -nP -iTCP:3000 -sTCP:LISTEN 2>/dev/null | sed 's/^/  /'; \
		exit 1; \
	fi
	@echo "$(YELLOW)→ Starting Postgres and Redis...$(RESET)"
	docker compose up -d postgres redis
	@echo "$(YELLOW)→ Running migrations...$(RESET)"
	$(MAKE) migrate
	@echo "$(YELLOW)→ Seeding demo data...$(RESET)"
	cd apps/api && APP_MODE=mock MARKET_DATA_PROVIDER=mock DISABLE_RATE_LIMITING=true ADMIN_EMAIL=admin@localhost ADMIN_PASSWORD=change-me PYTHONPATH=. $(PYTHON) -m app.db.seed
	@echo "$(YELLOW)→ Clearing local Redis rate-limit/cache state...$(RESET)"
	@REDIS_PASSWORD=$$(grep -E '^REDIS_PASSWORD=' .env 2>/dev/null | tail -1 | cut -d= -f2-); \
	if [ -n "$$REDIS_PASSWORD" ]; then \
		docker compose exec -T redis redis-cli -a "$$REDIS_PASSWORD" FLUSHDB >/dev/null; \
	elif docker compose exec -T redis redis-cli FLUSHDB >/dev/null 2>&1; then \
		true; \
	else \
		docker compose exec -T redis redis-cli -a cashguard_redis FLUSHDB >/dev/null; \
	fi
	@echo "$(YELLOW)→ Starting API with MARKET_DATA_PROVIDER=mock...$(RESET)"
	@set -e; \
	cd apps/api; \
	APP_MODE=mock \
	MARKET_DATA_PROVIDER=mock \
	DISABLE_RATE_LIMITING=true \
	ADMIN_EMAIL=admin@localhost \
	ADMIN_PASSWORD=change-me \
	uvicorn app.main:app --host 127.0.0.1 --port 8000 --no-access-log & \
	API_PID=$$!; \
	cleanup() { kill $$API_PID 2>/dev/null || true; wait $$API_PID 2>/dev/null || true; }; \
	trap cleanup EXIT INT TERM; \
	echo "$(YELLOW)  → Waiting for API readiness...$(RESET)"; \
	API_READY=0; \
	for i in $$(seq 1 30); do \
		if ! kill -0 $$API_PID 2>/dev/null; then \
			echo "$(RED)API exited before readiness.$(RESET)"; \
			exit 1; \
		fi; \
		if curl -sf http://127.0.0.1:8000/v1/health/ready >/dev/null 2>&1; then \
			API_READY=1; \
			break; \
		fi; \
		sleep 1; \
	done; \
	if [ "$$API_READY" != "1" ]; then \
		echo "$(RED)API did not become ready on port 8000.$(RESET)"; \
		exit 1; \
	fi; \
	echo "$(YELLOW)→ Running Playwright E2E...$(RESET)"; \
	cd $(CURDIR)/apps/web && \
		NEXT_PUBLIC_APP_MODE=mock \
		NEXT_PUBLIC_API_URL=http://127.0.0.1:8000 \
		E2E_ADMIN_EMAIL=admin@localhost \
		E2E_ADMIN_PASSWORD=change-me \
		npm run e2e -- --workers=1 --grep-invert "Operator dashboard — real-backend integration"
	@echo "$(GREEN)✓ E2E validation passed$(RESET)"

# ── Trading 212 demo app read-only ─────────────────────────────────────────────
T212_DEMO_API_PORT ?= 8003
T212_DEMO_WEB_PORT ?= 3003
T212_DEMO_DB_PATH  ?= /tmp/t212_demo_readonly.db
T212_DEMO_API_PID  ?= /tmp/t212_demo_readonly_api.pid
T212_DEMO_WEB_PID  ?= /tmp/t212_demo_readonly_web.pid

.PHONY: t212-demo-app-readonly t212-demo-app-readonly-connect t212-demo-app-readonly-check t212-demo-app-readonly-stop

t212-demo-app-readonly: ## Start Trading 212 demo app in read-only mode on API :8003 and web :3003
	@$(MAKE) t212-demo-app-readonly-stop >/dev/null 2>&1 || true
	@echo "$(YELLOW)→ Initialising Trading 212 demo read-only SQLite DB...$(RESET)"
	@rm -f $(T212_DEMO_DB_PATH)
	@cd apps/api && \
		INTEGRATION_DB_PATH=$(T212_DEMO_DB_PATH) \
		DATABASE_URL="sqlite+aiosqlite:///$(T212_DEMO_DB_PATH)" \
		APP_MODE=demo \
		ADMIN_EMAIL=admin@localhost \
		ADMIN_PASSWORD=change-me \
		PYTHONPATH=. \
		$(PYTHON) scripts/init_integration_db.py
	@echo "$(YELLOW)→ Starting API on :$(T212_DEMO_API_PORT) (APP_MODE=demo, read-only)...$(RESET)"
	@cd apps/api; \
		DATABASE_URL="sqlite+aiosqlite:///$(T212_DEMO_DB_PATH)" \
		APP_MODE=demo \
		T212_ENVIRONMENT=demo \
		LIVE_TRADING_ENABLED=false \
		MARKET_DATA_PROVIDER=mock \
		DISABLE_RATE_LIMITING=true \
		ADMIN_EMAIL=admin@localhost \
		ADMIN_PASSWORD=change-me \
		CORS_ORIGINS="http://localhost:$(T212_DEMO_WEB_PORT),http://127.0.0.1:$(T212_DEMO_WEB_PORT)" \
		PYTHONPATH=. \
		uvicorn app.main:app --host 127.0.0.1 --port $(T212_DEMO_API_PORT) --no-access-log \
		> /tmp/t212_demo_readonly_api.log 2>&1 & echo $$! > $(T212_DEMO_API_PID)
	@echo "$(YELLOW)  → Waiting for API readiness on :$(T212_DEMO_API_PORT)...$(RESET)"
	@for attempt in $$(seq 1 30); do \
		if curl -sf http://127.0.0.1:$(T212_DEMO_API_PORT)/v1/health/live >/dev/null 2>&1; then \
			echo "$(GREEN)  ✓ API ready$(RESET)"; \
			break; \
		fi; \
		if [ "$$attempt" = "30" ]; then \
			echo "$(RED)API did not become ready. See /tmp/t212_demo_readonly_api.log$(RESET)"; \
			exit 1; \
		fi; \
		sleep 2; \
	done
	@echo "$(YELLOW)→ Starting web app on :$(T212_DEMO_WEB_PORT) (NEXT_PUBLIC_APP_MODE=demo)...$(RESET)"
	@cd apps/web; \
		NEXT_PUBLIC_API_URL=http://127.0.0.1:$(T212_DEMO_API_PORT) \
		NEXT_PUBLIC_APP_MODE=demo \
		BASE_URL=http://localhost:$(T212_DEMO_WEB_PORT) \
		npx next dev -p $(T212_DEMO_WEB_PORT) \
		> /tmp/t212_demo_readonly_web.log 2>&1 & echo $$! > $(T212_DEMO_WEB_PID)
	@echo ""
	@echo "╔══════════════════════════════════════════════════════════╗"
	@echo "║      T212 CashGuard — Trading 212 Demo Read-only         ║"
	@echo "╚══════════════════════════════════════════════════════════╝"
	@echo ""
	@echo "  Frontend : http://localhost:$(T212_DEMO_WEB_PORT)"
	@echo "  Backend  : http://127.0.0.1:$(T212_DEMO_API_PORT)"
	@echo "  API docs : http://127.0.0.1:$(T212_DEMO_API_PORT)/docs"
	@echo ""
	@echo "  Login credentials"
	@echo "    Email    : admin@localhost"
	@echo "    Password : change-me"
	@echo ""
	@echo "  Pages to check"
	@echo "    Broker   : http://localhost:$(T212_DEMO_WEB_PORT)/app/broker"
	@echo "    Dashboard: http://localhost:$(T212_DEMO_WEB_PORT)/app/dashboard"
	@echo "    Positions: http://localhost:$(T212_DEMO_WEB_PORT)/app/positions"
	@echo "    Operator : http://localhost:$(T212_DEMO_WEB_PORT)/app/operator"
	@echo ""
	@echo "  Logs"
	@echo "    API : tail -f /tmp/t212_demo_readonly_api.log"
	@echo "    Web : tail -f /tmp/t212_demo_readonly_web.log"
	@echo ""
	@echo "  When finished run: make t212-demo-app-readonly-stop"


t212-demo-app-readonly-connect: ## Store Trading 212 demo credentials from T212_API_KEY/T212_API_SECRET into local demo DB
	@echo "$(YELLOW)→ Connecting Trading 212 demo credentials to local read-only demo DB...$(RESET)"
	@test -n "$$T212_API_KEY" || (echo "$(RED)T212_API_KEY is not loaded in this terminal.$(RESET)" && exit 1)
	@test -n "$$T212_API_SECRET" || (echo "$(RED)T212_API_SECRET is not loaded in this terminal.$(RESET)" && exit 1)
	@set -e; \
		login_tmp=$$(mktemp); \
		login_code=$$(curl -sS -o "$$login_tmp" -w '%{http_code}' -X POST http://127.0.0.1:$(T212_DEMO_API_PORT)/v1/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@localhost","password":"change-me"}' || true); \
		if [ "$$login_code" != "200" ]; then \
			echo "$(RED)/v1/auth/login -> $$login_code$(RESET)"; \
			cat "$$login_tmp"; echo; rm -f "$$login_tmp"; exit 1; \
		fi; \
		TOKEN=$$(python3 -c 'import sys,json; print(json.load(open(sys.argv[1]))["access_token"])' "$$login_tmp"); \
		rm -f "$$login_tmp"; \
		payload_tmp=$$(mktemp); \
		python3 -c 'import json, os, sys; json.dump({"api_key": os.environ["T212_API_KEY"].strip(), "api_secret": os.environ["T212_API_SECRET"].strip(), "environment": "demo"}, open(sys.argv[1], "w"))' "$$payload_tmp"; \
		out_tmp=$$(mktemp); \
		code=$$(curl -sS -o "$$out_tmp" -w '%{http_code}' -X POST http://127.0.0.1:$(T212_DEMO_API_PORT)/v1/broker/trading212/connect -H "Authorization: Bearer $$TOKEN" -H 'Content-Type: application/json' --data-binary "@$$payload_tmp" || true); \
		rm -f "$$payload_tmp"; \
		if [ "$$code" != "200" ]; then \
			echo "$(RED)/v1/broker/trading212/connect -> $$code$(RESET)"; \
			cat "$$out_tmp"; echo; rm -f "$$out_tmp"; exit 1; \
		fi; \
		python3 -c 'import json, sys; data=json.load(open(sys.argv[1])); print("  broker=" + str(data.get("broker"))); print("  environment=" + str(data.get("environment"))); print("  credential_state=" + str(data.get("credential_state"))); print("  last_test_ok=" + str(data.get("last_test_ok"))); print("  account_currency=" + str(data.get("account_currency")))' "$$out_tmp"; \
		rm -f "$$out_tmp"
	@echo "$(GREEN)✓ Trading 212 demo credentials connected locally$(RESET)"

t212-demo-app-readonly-check: ## Check Trading 212 demo read-only API endpoints on :8003
	@echo "$(YELLOW)→ Checking Trading 212 demo read-only API endpoints on :$(T212_DEMO_API_PORT)...$(RESET)"
	@set -e; \
		login_tmp=$$(mktemp); \
		login_code=$$(curl -sS -o "$$login_tmp" -w '%{http_code}' -X POST http://127.0.0.1:$(T212_DEMO_API_PORT)/v1/auth/login -H 'Content-Type: application/json' -d '{"email":"admin@localhost","password":"change-me"}' || true); \
		if [ "$$login_code" != "200" ]; then \
			echo "$(RED)/v1/auth/login -> $$login_code$(RESET)"; \
			cat "$$login_tmp"; echo; rm -f "$$login_tmp"; exit 1; \
		fi; \
		TOKEN=$$(python3 -c 'import sys,json; print(json.load(open(sys.argv[1]))["access_token"])' "$$login_tmp"); \
		rm -f "$$login_tmp"; \
		failed=0; \
		for endpoint in /v1/health/live /v1/broker/trading212/status /v1/account/summary /v1/positions /v1/account/cash-guard; do \
			tmp=$$(mktemp); \
			code=$$(curl -sS -o "$$tmp" -w '%{http_code}' -H "Authorization: Bearer $$TOKEN" http://127.0.0.1:$(T212_DEMO_API_PORT)$$endpoint || true); \
			echo "  $$endpoint -> $$code"; \
			if [ "$$code" != "200" ]; then \
				cat "$$tmp"; echo; failed=1; \
			fi; \
			rm -f "$$tmp"; \
		done; \
		if [ "$$failed" != "0" ]; then \
			echo "$(RED)Trading 212 demo read-only check failed.$(RESET)"; \
			exit 1; \
		fi
	@echo "$(GREEN)✓ Trading 212 demo read-only API endpoints returned 200$(RESET)"

t212-demo-app-readonly-stop: ## Stop Trading 212 demo read-only API/web servers
	@echo "$(YELLOW)→ Stopping Trading 212 demo read-only servers...$(RESET)"
	@for spec in "API:$(T212_DEMO_API_PID)" "Web:$(T212_DEMO_WEB_PID)"; do \
		name=$${spec%%:*}; \
		pidfile=$${spec#*:}; \
		if [ -f "$$pidfile" ]; then \
			pid=$$(cat "$$pidfile" 2>/dev/null || true); \
			if [ -n "$$pid" ] && kill -0 "$$pid" 2>/dev/null; then \
				kill "$$pid" 2>/dev/null || true; \
				echo "  ✓ $$name stopped"; \
			else \
				echo "  $$name PID file existed but process was not running"; \
			fi; \
			rm -f "$$pidfile"; \
		else \
			echo "  No $$name PID file"; \
		fi; \
	done
	@echo "$(GREEN)✓ Trading 212 demo read-only stop routine complete$(RESET)"

# ── Trading 212 controlled demo order test ─────────────────────────────────────
T212_DEMO_ORDER_API_PORT ?= 8004
T212_DEMO_ORDER_DB_PATH  ?= /tmp/t212_demo_controlled_order.db
T212_DEMO_ORDER_API_PID  ?= /tmp/t212_demo_controlled_order_api.pid
T212_DEMO_ORDER_TICKER   ?= AAPL
T212_DEMO_ORDER_QUANTITY ?= 0.001

.PHONY: t212-demo-controlled-order-start t212-demo-controlled-order-arm t212-demo-controlled-order-test t212-demo-reconcile-order t212-demo-reconciliation-worker t212-demo-controlled-order-stop

t212-demo-controlled-order-start: ## Start API for explicit Trading 212 demo order test on :8004
	@$(MAKE) t212-demo-controlled-order-stop >/dev/null 2>&1 || true
	@echo "$(YELLOW)→ Initialising Trading 212 controlled demo-order SQLite DB...$(RESET)"
	@rm -f $(T212_DEMO_ORDER_DB_PATH)
	@cd apps/api && \
		INTEGRATION_DB_PATH=$(T212_DEMO_ORDER_DB_PATH) \
		DATABASE_URL="sqlite+aiosqlite:///$(T212_DEMO_ORDER_DB_PATH)" \
		APP_MODE=demo \
		ADMIN_EMAIL=admin@localhost \
		ADMIN_PASSWORD=change-me \
		PYTHONPATH=. \
		$(PYTHON) scripts/init_integration_db.py
	@echo "$(YELLOW)→ Starting API on :$(T212_DEMO_ORDER_API_PORT) with controlled demo-order gate enabled...$(RESET)"
	@cd apps/api; \
		DATABASE_URL="sqlite+aiosqlite:///$(T212_DEMO_ORDER_DB_PATH)" \
		APP_MODE=demo \
		T212_ENVIRONMENT=demo \
		T212_DEMO_ORDER_ENABLED=true \
		LIVE_TRADING_ENABLED=false \
		MARKET_DATA_PROVIDER=mock \
		DISABLE_RATE_LIMITING=true \
		ADMIN_EMAIL=admin@localhost \
		ADMIN_PASSWORD=change-me \
		CORS_ORIGINS="http://localhost:3004,http://127.0.0.1:3004" \
		PYTHONPATH=. \
		uvicorn app.main:app --host 127.0.0.1 --port $(T212_DEMO_ORDER_API_PORT) --no-access-log \
		> /tmp/t212_demo_controlled_order_api.log 2>&1 & echo $$! > $(T212_DEMO_ORDER_API_PID)
	@echo "$(YELLOW)  → Waiting for API readiness on :$(T212_DEMO_ORDER_API_PORT)...$(RESET)"
	@for attempt in $$(seq 1 30); do \
		if curl -sf http://127.0.0.1:$(T212_DEMO_ORDER_API_PORT)/v1/health/live >/dev/null 2>&1; then \
			echo "$(GREEN)  ✓ API ready$(RESET)"; \
			break; \
		fi; \
		if [ "$$attempt" = "30" ]; then \
			echo "$(RED)API did not become ready. See /tmp/t212_demo_controlled_order_api.log$(RESET)"; \
			exit 1; \
		fi; \
		sleep 2; \
	done
	@echo ""
	@echo "Controlled Trading 212 DEMO order API is running on http://127.0.0.1:$(T212_DEMO_ORDER_API_PORT)"
	@echo "Logs: tail -f /tmp/t212_demo_controlled_order_api.log"
	@echo "When finished: make t212-demo-controlled-order-stop"


t212-demo-controlled-order-arm: ## Disable kill switch only in disposable controlled demo-order DB; requires explicit confirmation
	@echo "$(YELLOW)→ Arming disposable Trading 212 DEMO order test DB...$(RESET)"
	@test "$$T212_DEMO_ORDER_CONFIRM" = "PLACE_DEMO_ORDER" || (echo "$(RED)Set T212_DEMO_ORDER_CONFIRM=PLACE_DEMO_ORDER before arming demo-order test DB.$(RESET)" && exit 1)
	@cd apps/api && \
		T212_DEMO_ORDER_DB_PATH="$(T212_DEMO_ORDER_DB_PATH)" \
		PYTHONPATH=. \
		$(PYTHON) scripts/t212_demo_arm_controlled_order.py
	@echo "$(GREEN)✓ Controlled demo-order DB armed$(RESET)"

t212-demo-controlled-order-test: ## Place one tiny Trading 212 DEMO order; requires explicit env confirmation
	@echo "$(YELLOW)→ Running controlled Trading 212 DEMO order test...$(RESET)"
	@test "$$T212_DEMO_ORDER_CONFIRM" = "PLACE_DEMO_ORDER" || (echo "$(RED)Set T212_DEMO_ORDER_CONFIRM=PLACE_DEMO_ORDER to confirm this demo-order test.$(RESET)" && exit 1)
	@test -n "$$T212_API_KEY" || (echo "$(RED)T212_API_KEY is not loaded in this terminal.$(RESET)" && exit 1)
	@test -n "$$T212_API_SECRET" || (echo "$(RED)T212_API_SECRET is not loaded in this terminal.$(RESET)" && exit 1)
	@cd apps/api && \
		APP_MODE=demo \
		T212_ENVIRONMENT=demo \
		T212_DEMO_ORDER_ENABLED=true \
		T212_DEMO_ORDER_CONFIRM=PLACE_DEMO_ORDER \
		LIVE_TRADING_ENABLED=false \
		T212_DEMO_API_URL=http://127.0.0.1:$(T212_DEMO_ORDER_API_PORT) \
		T212_DEMO_ORDER_TICKER=$(T212_DEMO_ORDER_TICKER) \
		T212_DEMO_ORDER_QUANTITY=$(T212_DEMO_ORDER_QUANTITY) \
		PYTHONPATH=. \
		$(PYTHON) scripts/t212_demo_controlled_order.py
	@echo "$(GREEN)✓ Controlled Trading 212 DEMO order test passed$(RESET)"

t212-demo-reconcile-order: ## Reconcile one local Trading 212 DEMO order from read-only broker history
	@echo "$(YELLOW)→ Running Trading 212 DEMO order reconciliation...$(RESET)"
	@test "$$T212_DEMO_RECONCILE_CONFIRM" = "READ_DEMO_ORDER_HISTORY" || (echo "$(RED)Set T212_DEMO_RECONCILE_CONFIRM=READ_DEMO_ORDER_HISTORY to confirm this read-only demo history check.$(RESET)" && exit 1)
	@test -n "$$T212_API_KEY" || (echo "$(RED)T212_API_KEY is not loaded in this terminal.$(RESET)" && exit 1)
	@test -n "$$T212_API_SECRET" || (echo "$(RED)T212_API_SECRET is not loaded in this terminal.$(RESET)" && exit 1)
	@if [ -z "$$T212_DEMO_RECONCILE_ORDER_ID" ] && [ -z "$$T212_DEMO_RECONCILE_BROKER_ORDER_ID" ]; then \
		echo "$(RED)Set T212_DEMO_RECONCILE_ORDER_ID or T212_DEMO_RECONCILE_BROKER_ORDER_ID.$(RESET)"; \
		exit 1; \
	fi
	@test "$${LIVE_TRADING_ENABLED:-false}" != "true" || (echo "$(RED)LIVE_TRADING_ENABLED must be false.$(RESET)" && exit 1)
	@cd apps/api && \
		DATABASE_URL="$${DATABASE_URL:-sqlite+aiosqlite:///$(T212_DEMO_ORDER_DB_PATH)}" \
		APP_MODE=demo \
		T212_ENVIRONMENT=demo \
		LIVE_TRADING_ENABLED=false \
		MARKET_DATA_PROVIDER=mock \
		PYTHONPATH=. \
		$(PYTHON) scripts/t212_demo_reconcile_order.py
	@echo "$(GREEN)✓ Trading 212 DEMO order reconciliation finished$(RESET)"

t212-demo-reconciliation-worker: ## Run one read-only Trading 212 DEMO reconciliation worker pass
	@echo "$(YELLOW)→ Running Trading 212 DEMO reconciliation worker...$(RESET)"
	@test "$$T212_DEMO_RECONCILE_CONFIRM" = "READ_DEMO_ORDER_HISTORY" || (echo "$(RED)Set T212_DEMO_RECONCILE_CONFIRM=READ_DEMO_ORDER_HISTORY to confirm this read-only demo history check.$(RESET)" && exit 1)
	@test -n "$$T212_API_KEY" || (echo "$(RED)T212_API_KEY is not loaded in this terminal.$(RESET)" && exit 1)
	@test -n "$$T212_API_SECRET" || (echo "$(RED)T212_API_SECRET is not loaded in this terminal.$(RESET)" && exit 1)
	@test "$${LIVE_TRADING_ENABLED:-false}" != "true" || (echo "$(RED)LIVE_TRADING_ENABLED must be false.$(RESET)" && exit 1)
	@cd apps/api && \
		DATABASE_URL="$${DATABASE_URL:-sqlite+aiosqlite:///$(T212_DEMO_ORDER_DB_PATH)}" \
		APP_MODE=demo \
		T212_ENVIRONMENT=demo \
		LIVE_TRADING_ENABLED=false \
		DEMO_RECONCILIATION_WORKER_ENABLED=true \
		MARKET_DATA_PROVIDER=mock \
		PYTHONPATH=. \
		$(PYTHON) scripts/t212_demo_reconciliation_worker.py
	@echo "$(GREEN)✓ Trading 212 DEMO reconciliation worker finished$(RESET)"

t212-demo-controlled-order-stop: ## Stop controlled Trading 212 demo-order API
	@echo "$(YELLOW)→ Stopping Trading 212 controlled demo-order API...$(RESET)"
	@if [ -f "$(T212_DEMO_ORDER_API_PID)" ]; then \
		pid=$$(cat "$(T212_DEMO_ORDER_API_PID)" 2>/dev/null || true); \
		if [ -n "$$pid" ] && kill -0 "$$pid" 2>/dev/null; then \
			kill "$$pid" 2>/dev/null || true; \
			echo "  ✓ API stopped"; \
		else \
			echo "  API PID file existed but process was not running"; \
		fi; \
		rm -f "$(T212_DEMO_ORDER_API_PID)"; \
	else \
		echo "  No API PID file"; \
	fi
	@echo "$(GREEN)✓ Controlled demo-order stop routine complete$(RESET)"
