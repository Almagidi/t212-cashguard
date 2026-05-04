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

.PHONY: smoke readiness

smoke:
	cd apps/api && DATABASE_URL=sqlite+aiosqlite:///:memory: REDIS_URL=redis://localhost:6379/15 SECRET_KEY=test-secret-key-32-chars-minimum-x MASTER_KEY=test-master-key-32-chars-minimum-x APP_MODE=mock python3.12 -m pytest tests/smoke/ -v --tb=short --no-cov

readiness: smoke e2e-operator
