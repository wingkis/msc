.PHONY: build up down restart migrate shell logs test lint dev cli health clean

# Build Docker images
build:
	docker compose build

# Start services
up:
	docker compose up -d

# Rebuild the image and restart the container (the API is baked into the image)
restart:
	docker compose build
	docker compose up -d

# Stop services
down:
	docker compose down

# Run migration
migrate:
	python migrations/migrate_from_markdown.py

# Enter backend shell
shell:
	docker exec -it msc-api sh

# Check logs
logs:
	docker compose logs -f msc-api

# Run tests
test:
	python -m pytest backend/tests/ -v

# Correctness lint (unused imports/vars, undefined names, syntax errors)
lint:
	ruff check --select F,E9 backend/msc backend/tests

# Install the test + CLI tooling on the host
dev:
	pip install --user --break-system-packages -r backend/requirements-dev.txt
	pip install --user --break-system-packages -e cli

# The API CLI (talks to the running server over HTTP)
cli:
	pip install --user --break-system-packages -e cli

# What the container thinks of itself
health:
	@curl -s localhost:8000/api/v1/health | python3 -m json.tool

# Clean up
clean:
	docker compose down -v
	rm -rf data backups

# Setup: generate env, run migration, start
setup:
	@if [ ! -f .env ]; then cp .env.example .env; fi
	python migrations/migrate_from_markdown.py
	docker compose up -d
