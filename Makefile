APP=series-renamer

.PHONY: up down test

up:
	docker compose up -d --build

down:
	docker compose down

test:
	python3 -m pytest -q
	python3 -m py_compile app/main.py app/services.py
