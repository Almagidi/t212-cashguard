
.PHONY: help setup dev up down migrate seed reset test lint typecheck e2e e2e-operator e2e-operator-integration readiness-full logs clean operator-manual operator-manual-stop

.PHONY: help setup dev up down migrate seed reset test lint typecheck e2e e2e-operator logs clean

SHELL := /bin/bash
.DEFAULT_GOAL := help

GREEN  := \033[0;32m
YELLOW := \033[1;33m
RED    := \033[0;31m
RESET  := \033[0m

help: ## Show this help message
	@echo ""
	@echo "  T212 CashGuard Trader — Local Development Commands"
	@echo ""
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  $(GREEN)%-20s$(RESET) %s\n", $$1, $$2}'
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

.PHONY: smoke readiness e2e-operator-integration readiness-full

smoke:
	cd apps/api && DATABASE_URL=sqlite+aiosqlite:///:memory: REDIS_URL=redis://localhost:6379/15 SECRET_KEY=test-secret-key-32-chars-minimum-x MASTER_KEY=test-master-key-32-chars-minimum-x APP_MODE=mock python3.12 -m pytest tests/smoke/ -v --tb=short --no-cov

readiness: smoke e2e-operator

e2e-operator-integration: ## Run real-backend integration e2e for operator dashboard (SQLite, APP_MODE=mock, ports 8001/3001)
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
	cd apps/api && \
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
	trap "kill $$API_PID 2>/dev/null || true" EXIT INT TERM; \
	echo "$(YELLOW)  → Waiting for API to be ready...$(RESET)"; \
	for i in $$(seq 1 20); do \
		curl -sf http://127.0.0.1:8001/v1/health/live >/dev/null 2>&1 && break || true; \
		sleep 1; \
	done; \
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

# ─── Manual QA — local real-backend in mock mode ──────────────────────────────
MANUAL_QA_API_PORT ?= 8002
MANUAL_QA_WEB_PORT ?= 3002
MANUAL_QA_DB_PATH  ?= /tmp/t212_manual_qa.db
MANUAL_QA_API_PID  ?= /tmp/t212_manual_api.pid
MANUAL_QA_WEB_PID  ?= /tmp/t212_manual_web.pid

operator-manual: ## Start local manual QA servers (API :8002, web :3002, APP_MODE=mock, no broker creds needed)
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
	@echo "$(YELLOW)→ Starting API on :$(MANUAL_QA_API_PORT) (APP_MODE=mock, background)...$(RESET)"
	@( cd apps/api && exec env \
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
	) >> /tmp/t212_manual_api.log 2>&1 & echo $$! > $(MANUAL_QA_API_PID)
	@echo "$(YELLOW)  → Waiting for API to be ready on :$(MANUAL_QA_API_PORT)...$(RESET)"
	@for i in $$(seq 1 30); do \
		curl -sf http://127.0.0.1:$(MANUAL_QA_API_PORT)/v1/health/live >/dev/null 2>&1 && break || true; \
		sleep 1; \
	done
	@echo "$(YELLOW)→ Starting web app on :$(MANUAL_QA_WEB_PORT) (background)...$(RESET)"
	@( cd apps/web && exec env \
		NEXT_PUBLIC_API_URL=http://127.0.0.1:$(MANUAL_QA_API_PORT) \
		NEXT_PUBLIC_APP_MODE=mock \
		npx next dev -p $(MANUAL_QA_WEB_PORT) \
	) >> /tmp/t212_manual_web.log 2>&1 & echo $$! > $(MANUAL_QA_WEB_PID)
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
	@echo "  $(YELLOW)Note: web app may take 15–30 s to compile on first load.$(RESET)"
	@echo "  $(YELLOW)When finished run: make operator-manual-stop$(RESET)"
	@echo ""
	@echo "  See docs/operator-manual-qa.md for the full QA checklist."
	@echo ""

operator-manual-stop: ## Stop local manual QA servers started by operator-manual
	@echo "$(YELLOW)→ Stopping manual QA servers...$(RESET)"
	@if [ -f $(MANUAL_QA_API_PID) ]; then \
		kill $$(cat $(MANUAL_QA_API_PID)) 2>/dev/null \
			&& echo "  $(GREEN)✓ API stopped$(RESET)" \
			|| echo "  API process already gone"; \
		rm -f $(MANUAL_QA_API_PID); \
	else \
		lsof -ti tcp:$(MANUAL_QA_API_PORT) | xargs kill 2>/dev/null \
			&& echo "  $(GREEN)✓ API port $(MANUAL_QA_API_PORT) cleared$(RESET)" \
			|| echo "  Nothing on port $(MANUAL_QA_API_PORT)"; \
	fi
	@if [ -f $(MANUAL_QA_WEB_PID) ]; then \
		kill $$(cat $(MANUAL_QA_WEB_PID)) 2>/dev/null \
			&& echo "  $(GREEN)✓ Web stopped$(RESET)" \
			|| echo "  Web process already gone"; \
		rm -f $(MANUAL_QA_WEB_PID); \
	else \
		lsof -ti tcp:$(MANUAL_QA_WEB_PORT) | xargs kill 2>/dev/null \
			&& echo "  $(GREEN)✓ Web port $(MANUAL_QA_WEB_PORT) cleared$(RESET)" \
			|| echo "  Nothing on port $(MANUAL_QA_WEB_PORT)"; \
	fi
	@echo "$(GREEN)✓ Manual QA servers stopped$(RESET)"
