# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY packages/connector-sdk/pyproject.toml /connector-sdk/pyproject.toml
COPY packages/connector-sdk/src /connector-sdk/src
RUN pip install --no-cache-dir /connector-sdk

COPY apps/api/pyproject.toml ./pyproject.toml
COPY apps/api/src ./src
RUN pip install --no-cache-dir ".[dev]"

COPY apps/api/alembic.ini ./alembic.ini
COPY apps/api/migrations ./migrations
COPY apps/api/tests_database ./tests_database

CMD ["sh", "-c", "black --check src migrations tests_database && ruff check src migrations tests_database && mypy src migrations tests_database && pytest -o addopts='' tests_database"]
