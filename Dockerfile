# Необязательный способ запуска: веб-интерфейс wwp с pandoc и pdftotext.
# Консольные провайдеры (codex, claude, opencode) в образ не входят — для них запуск через uv на хосте.
FROM python:3.12-slim
RUN apt-get update && apt-get install -y --no-install-recommends pandoc poppler-utils git \
    && apt-get clean && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src ./src
COPY defaults ./defaults
RUN uv sync --frozen --no-dev
ENV WWP_WORKSPACE=/workspace PATH="/app/.venv/bin:$PATH"
CMD ["sh", "-c", "wwp init && wwp serve --host 127.0.0.1 --port 8765"]
