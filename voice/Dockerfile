# syntax=docker/dockerfile:1
FROM python:3.13-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv \
    UV_COMPILE_BYTECODE=1

# System deps: psycopg build + pdf/image tooling (pdfplumber/pillow).
RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

WORKDIR /app

# Install ONLY the locked runtime deps into /app/.venv (project itself is not a
# package — see [tool.uv] package=false). Layer-cached on lockfile changes.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev
ENV PATH="/app/.venv/bin:$PATH"

COPY . .
# DEBUG=1 only for this build step so the prod fail-closed check (settings.py raises
# on dev-secret / missing Vapi secrets when DEBUG=0) doesn't abort collectstatic.
RUN DJANGO_DEBUG=1 python manage.py collectstatic --noinput

EXPOSE 8000
CMD ["gunicorn", "config.wsgi:application", "--bind", "0.0.0.0:8000", "--workers", "3", "--timeout", "120"]
