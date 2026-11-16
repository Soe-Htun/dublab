#!/usr/bin/env bash
# Start DubLab with waitress. Usage: ./run.sh  (override with PORT=9000 THREADS=4 ./run.sh)
set -e
cd "$(dirname "$0")"
[ -f venv/bin/activate ] && source venv/bin/activate
[ -f .env ] && { set -a; source .env; set +a; }
# waitress rejects request bodies over 1 GB by default; follow DUBLAB_MAX_MB (+16 MB form overhead)
MAX_BODY=$(( (${DUBLAB_MAX_MB:-3000} + 16) * 1024 * 1024 ))
exec waitress-serve --listen="${HOST:-127.0.0.1}:${PORT:-8092}" --threads="${THREADS:-8}" \
  --max-request-body-size="$MAX_BODY" app:app
