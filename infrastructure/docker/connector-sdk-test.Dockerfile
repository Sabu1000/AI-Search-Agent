# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /sdk

COPY packages/connector-sdk/pyproject.toml ./pyproject.toml
COPY packages/connector-sdk/src ./src

RUN pip install --no-cache-dir ".[dev]"

COPY packages/connector-sdk/tests ./tests

CMD ["sh", "-c", "black --check src tests && ruff check src tests && mypy src tests && pytest"]
