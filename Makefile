.DEFAULT_GOAL := help
.PHONY: help install up down logs migrate revision test check fmt demo e2e

COMPOSE := docker compose
BACKEND := cd backend &&
FRONTEND := cd frontend &&

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-10s %s\n", $$1, $$2}'

install: ## Install backend (uv) and frontend (npm) dependencies locally
	$(BACKEND) uv sync
	$(FRONTEND) npm ci

up: ## Build and start db, redis, api, worker, web
	$(COMPOSE) up -d --build

down: ## Stop all services
	$(COMPOSE) down

logs: ## Tail service logs
	$(COMPOSE) logs -f

migrate: ## Apply Alembic migrations (inside the api container)
	$(COMPOSE) run --rm api alembic upgrade head

revision: ## Autogenerate a migration: make revision m="add prices_daily"
	$(COMPOSE) run --rm -v $(CURDIR)/backend/app/db/alembic/versions:/app/app/db/alembic/versions \
		api alembic revision --autogenerate -m "$(m)"

test: ## Run backend tests (DB tests need Postgres at TEST_DATABASE_URL; `make up` provides one)
	$(BACKEND) uv run pytest

check: ## Lint + type-check backend (ruff, mypy --strict) and frontend (eslint, tsc)
	$(BACKEND) uv run ruff check .
	$(BACKEND) uv run ruff format --check .
	$(BACKEND) uv run mypy app
	$(FRONTEND) npm run lint
	$(FRONTEND) npm run typecheck

demo: ## Seed synthetic DEMO* stocks and build their reports (local dev DB only)
	$(COMPOSE) run --rm api python -m app.devtools.demo

e2e: ## Playwright UI tests against the running stack + demo data (E2E_PASSWORD=<APP_PASSWORD>)
	$(FRONTEND) npm run e2e

fmt: ## Auto-format backend code
	$(BACKEND) uv run ruff check --fix .
	$(BACKEND) uv run ruff format .
