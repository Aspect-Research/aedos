# Fly.io image for the Aedos deploy backend (deploy/backend/server.py).
# Build context is the REPO ROOT: flyctl deploy -c deploy/fly/fly.toml
#
# The runtime DB is NOT baked in — the entrypoint initializes a fresh
# schema + predicate-translation seeds on the mounted volume when absent.

FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir .

COPY deploy ./deploy
COPY scripts/reset_db.py ./scripts/reset_db.py
COPY seeds ./seeds
RUN chmod +x /app/deploy/fly/entrypoint.sh

ENV AEDOS_DB_PATH=/data/aedos.db \
    PYTHONUNBUFFERED=1

EXPOSE 8000
ENTRYPOINT ["/app/deploy/fly/entrypoint.sh"]
