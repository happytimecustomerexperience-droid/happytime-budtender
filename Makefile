.PHONY: build up down logs migrate shell sync test

build:
	docker compose build

up:
	docker compose up -d

down:
	docker compose down

logs:
	docker compose logs -f web celery-worker celery-beat

migrate:
	docker compose run --rm web python manage.py migrate

shell:
	docker compose run --rm web python manage.py shell

# Manually trigger a Dutchie inventory sync for all stores.
sync:
	docker compose run --rm web python manage.py shell -c "from budtender.tasks import sync_inventory; [sync_inventory(s) for s in ['yakima','mount-vernon','pullman']]"

test:
	docker compose run --rm web python manage.py test budtender
