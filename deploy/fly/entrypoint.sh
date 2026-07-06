#!/bin/sh
# First boot on an empty volume: create the schema and load the tracked
# predicate-translation seeds. A pre-warmed substrate DB can replace the
# fresh file later (fly sftp) — same path, server picks it up on restart.
set -e

DB="${AEDOS_DB_PATH:-/data/aedos.db}"

if [ ! -f "$DB" ]; then
  echo "aedos: initializing fresh DB at $DB"
  python scripts/reset_db.py "$DB"
  python seeds/load_seeds.py --db-path "$DB"
fi

exec python -m uvicorn deploy.backend.server:create_app --factory \
  --host 0.0.0.0 --port 8000
