# syntax=docker/dockerfile:1
FROM python:3.13-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# uv for fast, reproducible installs
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install deps first (cached layer)
COPY pyproject.toml ./
RUN uv pip install --system --no-cache -r pyproject.toml

# App code
COPY . .

# Non-root runtime user
RUN useradd --create-home --uid 10001 app \
    && chown -R app:app /app
USER app

EXPOSE 8000

# Default: gunicorn WSGI (gthread for I/O concurrency). Overridden per-service in compose.
CMD ["gunicorn", "core.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "5", "--worker-class", "gthread", "--threads", "4", "--timeout", "60", "--keep-alive", "5"]
