#!/usr/bin/env bash
# Start DubLab with waitress. Usage: ./run.sh  (override with PORT=9000 THREADS=4 ./run.sh)
set -e
cd "$(dirname "$0")"
[ -f venv/bin/activate ] && source venv/bin/activate
# Load .env as plain KEY=value lines (values may contain spaces and are not
# run as shell code; surrounding quotes are optional). Same format as
# `docker run --env-file`.
if [ -f .env ]; then
  while IFS= read -r line || [ -n "$line" ]; do
    case "$line" in ''|\#*) continue ;; esac
    key="${line%%=*}"; val="${line#*=}"
    [[ "$key" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]] || continue
    if [[ "$val" == \"*\" || "$val" == \'*\' ]]; then val="${val:1:${#val}-2}"; fi
    export "$key=$val"
  done < .env
fi
# waitress rejects request bodies over 1 GB by default; follow DUBLAB_MAX_MB (+16 MB form overhead)
MAX_BODY=$(( (${DUBLAB_MAX_MB:-3000} + 16) * 1024 * 1024 ))
exec waitress-serve --listen="${HOST:-127.0.0.1}:${PORT:-8092}" --threads="${THREADS:-8}" \
  --max-request-body-size="$MAX_BODY" app:app
