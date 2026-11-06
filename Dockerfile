# --- base: everything the app needs to run ---
FROM python:3.13-slim AS base

# Don't write .pyc files, and print logs immediately instead of buffering them
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /code

# Install dependencies first so Docker can cache this layer between builds
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY alembic.ini .
COPY alembic ./alembic
COPY app ./app

# --- test: base plus test tools and the tests (used by the "tests" service) ---
FROM base AS test

COPY requirements-dev.txt .
RUN pip install --no-cache-dir -r requirements-dev.txt

COPY pytest.ini .
COPY tests ./tests

CMD ["pytest"]

# --- production: what the api service runs (no test tools or tests) ---
FROM base AS production

COPY scripts/start.sh ./scripts/start.sh

# Documents the default port; the real one comes from $PORT (see start.sh)
EXPOSE 8000

# Runs migrations, then starts uvicorn
CMD ["sh", "scripts/start.sh"]
