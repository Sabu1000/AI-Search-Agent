# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app

COPY packages/connector-sdk/pyproject.toml /connector-sdk/pyproject.toml
COPY packages/connector-sdk/src /connector-sdk/src
RUN pip install --no-cache-dir /connector-sdk

COPY apps/api/pyproject.toml ./pyproject.toml
COPY apps/api/src ./src
RUN pip install --no-cache-dir .

COPY apps/api/alembic.ini ./alembic.ini
COPY apps/api/migrations ./migrations

FROM base AS test

COPY apps/api/tests ./tests
RUN pip install --no-cache-dir ".[dev]" \
    && black --check src tests \
    && ruff check src tests \
    && mypy src \
    && pytest

FROM base AS runtime

RUN addgroup --system app \
    && adduser --system --ingroup app --home /app app \
    && chown -R app:app /app

USER app
EXPOSE 8000

CMD ["uvicorn", "universal_ai_search.api.app:app", "--host", "0.0.0.0", "--port", "8000"]
