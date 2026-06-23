.PHONY: install check migrate makemigrations run test lint format provision up down logs

install:
	uv sync

check:
	uv run python manage.py check

makemigrations:
	uv run python manage.py makemigrations

migrate:
	uv run python manage.py migrate

run:
	uv run python manage.py runserver

test:
	uv run pytest

lint:
	uv run ruff check .

format:
	uv run ruff format --check .

# Idempotent Vapi reconcile (squad + assistants + tools + phone). See 20-SPEC-vapi-deploy.md.
provision:
	uv run python tools/provision_vapi.py

up:
	docker compose up --build

down:
	docker compose down

logs:
	docker compose logs -f web
