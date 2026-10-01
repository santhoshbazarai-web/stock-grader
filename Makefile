.DEFAULT_GOAL := help
.PHONY: help install up down logs migrate revision test check fmt demo e2e \
	acceptance acceptance-offline offline-seed \
	prod-config prod-up prod-down prod-logs prod-ps prod-backup prod-restore \
	home-config home-up home-down home-logs home-ps home-backup home-restore doctor doctor-local

COMPOSE := docker compose
BACKEND := cd backend &&
FRONTEND := cd frontend &&

help: ## List targets
	@grep -E '^[a-z-]+:.*## ' $(MAKEFILE_LIST) | awk -F':.*## ' '{printf "  %-13s %s\n", $$1, $$2}'

install: ## Install backend (uv) and frontend (npm) dependencies locally
	$(BACKEND) uv sync --extra camelot
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

acceptance: ## v1 acceptance on real data: search, pipeline, full report for 5 golden stocks (needs NSE access)
	$(FRONTEND) E2E_ACCEPTANCE=live npx playwright test e2e/acceptance.spec.ts

offline-seed: ## Seed the 5 synthetic offline-exchange companies (local dev DB only)
	$(BACKEND) uv run python -m app.devtools.offline_exchange seed

acceptance-offline: offline-seed ## v1 acceptance on the synthetic offline exchange (worker: OFFLINE_EXCHANGE=1)
	$(FRONTEND) E2E_ACCEPTANCE=offline npx playwright test e2e/acceptance.spec.ts

fmt: ## Auto-format backend code
	$(BACKEND) uv run ruff check --fix .
	$(BACKEND) uv run ruff format .

# ───────────── production (docker-compose.prod.yml + .env.production, see docs/DEPLOY.md) ─────────────
PROD := docker compose -f docker-compose.prod.yml --env-file .env.production

prod-config: ## Validate the production compose file and .env.production
	@test -f .env.production || { echo ".env.production missing: cp .env.production.example .env.production"; exit 1; }
	$(PROD) config --quiet && echo "compose config OK"

prod-up: prod-config ## Build and start (or update) the production stack; migrations run first
	$(PROD) up -d --build --wait

prod-down: ## Stop the production stack (volumes and backups are kept)
	$(PROD) down

prod-logs: ## Tail production logs: make prod-logs s=api
	$(PROD) logs -f --tail=200 $(s)

prod-ps: ## Production service status and health
	$(PROD) ps

prod-backup: ## Take a Postgres backup now (into BACKUP_PATH, default ./backups)
	$(PROD) exec -T backup /backup/backup.sh

prod-restore: ## Restore a backup: make prod-restore file=backups/daily/stockgrader-....dump
	ENV_FILE=.env.production deploy/restore.sh "$(file)"

# ───────────── home (docker-compose.home.yml + .env.home, SPEC §3.10, README "Home deployment") ─────────────
HOME_STACK := docker compose -f docker-compose.home.yml --env-file .env.home

home-config: ## Validate the home compose file and .env.home
	@test -f .env.home || { echo ".env.home missing: cp .env.home.example .env.home"; exit 1; }
	$(HOME_STACK) config --quiet && echo "compose config OK"

home-up: home-config ## Build and start (or update) the home stack on 127.0.0.1; migrations run first
	$(HOME_STACK) up -d --build --wait

home-down: ## Stop the home stack (data and backups are kept)
	$(HOME_STACK) down

home-logs: ## Tail home logs: make home-logs s=worker
	$(HOME_STACK) logs -f --tail=200 $(s)

home-ps: ## Home service status and health
	$(HOME_STACK) ps

home-backup: ## Take a Postgres backup now (into BACKUP_PATH)
	$(HOME_STACK) exec -T backup /backup/backup.sh

home-restore: ## Restore a backup: make home-restore file=/mnt/d/stock-grader-backups/daily/stockgrader-....dump
	COMPOSE_FILE=docker-compose.home.yml ENV_FILE=.env.home deploy/restore.sh "$(file)"

doctor: home-config ## Check the home stack: env, config, DB, Redis, broker tokens, NSE, disk, backups, worker
	$(HOME_STACK) --profile tools run --rm --no-deps doctor

doctor-local: ## The same checks against a local (non-Docker) setup, using backend/.env or the shell env
	$(BACKEND) uv run python -m app.doctor
