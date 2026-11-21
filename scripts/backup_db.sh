#!/usr/bin/env bash
# Transaction-consistent MySQL dump. Failed dumps never replace a valid backup.
set -euo pipefail
umask 077
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
BACKUP_DIR="$PROJECT_ROOT/data/backups"
mkdir -p "$BACKUP_DIR"
PARAM_FILE="$(mktemp "$BACKUP_DIR/.params.XXXXXX")"
TEMP_DUMP="$(mktemp "$BACKUP_DIR/.dump.XXXXXX")"
DUMP_ERRORS="$(mktemp "$BACKUP_DIR/.errors.XXXXXX")"
cleanup() { rm -f "$PARAM_FILE" "$TEMP_DUMP" "$DUMP_ERRORS"; }
trap cleanup EXIT
PYTHON_BIN="${PYTHON_BIN:-python3}"
"$PYTHON_BIN" - "$PROJECT_ROOT" > "$PARAM_FILE" <<'PY'
import os, sys
from pathlib import Path
from urllib.parse import urlsplit, unquote
try:
    from dotenv import dotenv_values
    settings = dotenv_values(Path(sys.argv[1]) / '.env', interpolate=False)
except ImportError:
    # No shell sourcing: .env contents cannot execute commands.
    settings = {}
    import shlex
    db_keys = {'DATABASE_URL', 'MYSQL_HOST', 'MYSQL_PORT', 'MYSQL_USER', 'MYSQL_PASSWORD', 'MYSQL_DATABASE'}
    path = Path(sys.argv[1]) / '.env'
    for line in path.read_text().splitlines() if path.exists() else []:
        line = line.strip().removeprefix('export ')
        if not line or line.startswith('#') or '=' not in line:
            continue
        key, value = line.split('=', 1)
        key = key.strip()
        if key not in db_keys:
            continue
        try:
            tokens = shlex.split(value, comments=True)
            settings[key] = tokens[0] if tokens else ''
        except ValueError:
            stripped = value.strip()
            if len(stripped) >= 2 and stripped[0] == stripped[-1] and stripped[0] in ('"', "'"):
                stripped = stripped[1:-1]
            settings[key] = stripped
settings.update(os.environ)
url = settings.get('DATABASE_URL', '').strip()
parsed = urlsplit(url) if url else None
if parsed and not parsed.scheme.startswith('mysql'):
    sys.exit('Backup requires a MySQL database URL')
def choose(key, value, default):
    # Explicit process MYSQL_* overrides URL; a process URL overrides file defaults.
    return os.environ.get(key) or (value if os.environ.get('DATABASE_URL') else settings.get(key) or value) or default
host = choose('MYSQL_HOST', parsed.hostname if parsed else None, '127.0.0.1')
port = choose('MYSQL_PORT', str(parsed.port or 3306) if parsed else None, '3306')
user = choose('MYSQL_USER', unquote(parsed.username or '') if parsed else None, 'douyin')
password = choose('MYSQL_PASSWORD', unquote(parsed.password or '') if parsed else None, '')
database = choose('MYSQL_DATABASE', unquote(parsed.path.lstrip('/')) if parsed else None, 'douyin_live')
for value in [host, port, user, password, database]:
    if '\0' in value or '\n' in value:
        sys.exit('Invalid database connection parameters')
    sys.stdout.buffer.write(value.encode() + b'\0')
PY
mapfile -d '' -t PARAMS < "$PARAM_FILE"
DB_HOST="${PARAMS[0]}"
DB_PORT="${PARAMS[1]}"
DB_USER="${PARAMS[2]}"
export MYSQL_PWD="${PARAMS[3]}"
DB_NAME="${PARAMS[4]}"
rm -f "$PARAM_FILE"
DUMP_FLAGS=(--single-transaction --quick --routines --triggers)
# MariaDB clients do not support MySQL's --ssl-mode/--column-statistics.
# Do not inject either flag unless the client advertises support.
local_dump() {
    local help_text
    help_text="$(mysqldump --help 2>/dev/null)" || return 1
    local flags=("${DUMP_FLAGS[@]}")
    if [[ "$help_text" == *column-statistics* ]]; then flags+=(--column-statistics=0); fi
    if [[ "$help_text" == *no-tablespaces* ]]; then flags+=(--no-tablespaces); fi
    if mysqldump "${flags[@]}" --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" -- "$DB_NAME" 2>"$DUMP_ERRORS" | gzip > "$TEMP_DUMP"; then
        return 0
    fi
    # MariaDB may default to TLS with verification against MySQL's generated
    # certificate. Retry only a TLS failure and only an advertised client flag.
    if [[ "$help_text" == *ssl-verify-server-cert* ]] && grep -Eqi 'TLS/SSL|SSL error|certificate verify' "$DUMP_ERRORS"; then
        mysqldump "${flags[@]}" --skip-ssl --host="$DB_HOST" --port="$DB_PORT" --user="$DB_USER" -- "$DB_NAME" 2>/dev/null | gzip > "$TEMP_DUMP"
    else
        return 1
    fi
}
DUMP_OK=false
if command -v mysqldump >/dev/null 2>&1; then
    if local_dump; then DUMP_OK=true; fi
fi
if [[ "$DUMP_OK" != true ]]; then
    if ! command -v docker >/dev/null 2>&1; then
        echo 'Database backup failed: no working dump client' >&2
        exit 1
    fi
    cd "$PROJECT_ROOT"
    if ! docker compose exec -T -e MYSQL_PWD="$MYSQL_PWD" db mysqldump "${DUMP_FLAGS[@]}" --no-tablespaces --user="$DB_USER" -- "$DB_NAME" 2>/dev/null | gzip > "$TEMP_DUMP"; then
        echo 'Database backup failed' >&2
        exit 1
    fi
fi
if ! gzip -t "$TEMP_DUMP"; then
    echo 'Database backup failed integrity check' >&2
    exit 1
fi
FINAL_DUMP="$BACKUP_DIR/douyin_live_$(date +%Y%m%d_%H%M%S).sql.gz"
# Serialize rapid calls within one second without overwriting a prior backup.
while [[ -e "$FINAL_DUMP" ]]; do
    sleep 1
    FINAL_DUMP="$BACKUP_DIR/douyin_live_$(date +%Y%m%d_%H%M%S).sql.gz"
done
mv "$TEMP_DUMP" "$FINAL_DUMP"
find "$BACKUP_DIR" -maxdepth 1 -type f -name 'douyin_live_*.sql.gz' -mmin +20160 -delete
printf 'Database backup completed: %s\n' "$(basename "$FINAL_DUMP")"
