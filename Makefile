.PHONY: help dev install test lint temporal worker webhook run ask onboard evals mcp docker-build

USER_ID ?= $(shell grep GROCERY_BUDDY_USER_ID .env.local 2>/dev/null | cut -d= -f2)

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
	@echo ""
	@echo "  Set USER_ID: export USER_ID=<your-uuid>  or add GROCERY_BUDDY_USER_ID=<uuid> to .env.local"

install: ## Install all dependencies
	uv sync

test: ## Run unit tests
	uv run pytest tests/ -v

lint: ## Run ruff linter
	uv run ruff check src/ tests/
	uv run ruff format --check src/ tests/

format: ## Auto-format code
	uv run ruff format src/ tests/
	uv run ruff check --fix src/ tests/

temporal: ## Start local Temporal server (Docker)
	docker compose up -d
	@echo "Temporal UI → http://localhost:8088"

temporal-stop: ## Stop local Temporal server
	docker compose down

worker: ## Start the Temporal worker
	uv run grocery-buddy worker

webhook: ## Start the approval webhook server (port 8080)
	uv run grocery-buddy webhook --port 8080

seed: ## Create your user record (EMAIL and NAME required)
	@[ "$(EMAIL)" ] || (echo "Usage: make seed EMAIL=you@example.com NAME='Your Name'"; exit 1)
	uv run python scripts/seed_user.py --email "$(EMAIL)" --name "$(NAME)"

amazon-setup: ## Save Amazon login session (interactive browser)
	AMAZON_HEADLESS=false uv run python scripts/setup_amazon_session.py

onboard: ## Run conversational onboarding (USER_ID required)
	@[ "$(USER_ID)" ] || (echo "Set USER_ID=<uuid>"; exit 1)
	uv run grocery-buddy onboard --user-id "$(USER_ID)"

run: ## Trigger one grocery run (USER_ID required)
	@[ "$(USER_ID)" ] || (echo "Set USER_ID=<uuid>"; exit 1)
	uv run grocery-buddy run --user-id "$(USER_ID)"

ask: ## Talk to the agent (USER_ID + MSG required), e.g. make ask MSG="I need eggs early"
	@[ "$(USER_ID)" ] || (echo "Set USER_ID=<uuid>"; exit 1)
	@[ "$(MSG)" ] || (echo "Usage: make ask MSG='I need eggs early'"; exit 1)
	uv run grocery-buddy ask --user-id "$(USER_ID)" $(MSG)

schedule: ## Set daily 8am schedule (USER_ID required)
	@[ "$(USER_ID)" ] || (echo "Set USER_ID=<uuid>"; exit 1)
	uv run grocery-buddy schedule --user-id "$(USER_ID)" --cron "0 8 * * *"

evals: ## Run prediction accuracy evals (USER_ID required)
	@[ "$(USER_ID)" ] || (echo "Set USER_ID=<uuid>"; exit 1)
	uv run grocery-buddy evals --user-id "$(USER_ID)"

mcp: ## Start MCP server (for Claude Code local dev)
	uv run grocery-buddy mcp

docker-build: ## Build the Docker image
	docker build -t grocery-buddy .

dev: ## Print dev startup checklist
	@echo ""
	@echo "── Dev startup checklist ─────────────────────────────"
	@echo "  1. make temporal          (start Temporal)"
	@echo "  2. make worker            (in a new terminal)"
	@echo "  3. make webhook           (in a new terminal)"
	@echo "  4. ngrok http 8080        (in a new terminal)"
	@echo "  5. Update WEBHOOK_BASE_URL in .env with ngrok URL"
	@echo "  6. make run USER_ID=<uuid>"
	@echo "  7. Temporal UI → http://localhost:8088"
	@echo ""
