.DEFAULT_GOAL := help
.PHONY: help install run lint format test check up-local down-local clean

PORT ?= 8765

help: ## Показать список целей
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) \
	  | awk 'BEGIN {FS = ":.*## "}; {printf "  \033[36m%-16s\033[0m %s\n", $$1, $$2}'

# --- Установка ---

install: ## Установить зависимости и создать workspace/
	uv sync
	uv run wwp init

# --- Запуск ---

run: ## Запустить веб-интерфейс (порт 8765, другой: make run PORT=8766)
	uv run wwp serve --port $(PORT)

# --- Проверка ---

lint: ## Проверить стиль (ruff) и типы (mypy)
	uv run ruff check .
	uv run mypy src

format: ## Отформатировать код (ruff)
	uv run ruff check --fix .
	uv run ruff format .

test: ## Запустить тесты
	uv run pytest -q

check: lint test ## Все проверки, как в CI

# --- Docker ---

up-local: ## Собрать и запустить контейнер (docker compose)
	docker compose up --build -d

down-local: ## Остановить контейнер
	docker compose down

# --- Обслуживание ---

clean: ## Удалить кэши и отчёт покрытия
	rm -rf .pytest_cache .ruff_cache .mypy_cache .coverage htmlcov
	find . -name __pycache__ -type d -not -path './.venv/*' -prune -exec rm -rf {} +
