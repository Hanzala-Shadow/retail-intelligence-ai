#!/usr/bin/env bash
set -Eeuo pipefail

# Run a repository command through PostgreSQL peer authentication without
# leaving a permanent login role behind. This wrapper is intentionally strict:
# it only manages the absent `ubuntu` role and grants read-only catalogue/data
# access for the duration of one command.

ROLE_NAME="ubuntu"
DB_NAME="${PGDATABASE:-retail_intelligence}"
ROLE_CREATED=0

usage() {
  echo "Usage: PGDATABASE=<database> $0 -- <command> [arguments...]" >&2
}

if [[ $# -lt 2 || "$1" != "--" ]]; then
  usage
  exit 64
fi
shift

if [[ "$(id -un)" != "ubuntu" ]]; then
  echo "ERROR: this wrapper must be run by the Linux user ubuntu" >&2
  exit 65
fi

if [[ ! "$DB_NAME" =~ ^[A-Za-z_][A-Za-z0-9_]*$ ]]; then
  echo "ERROR: unsafe PostgreSQL database name: $DB_NAME" >&2
  exit 66
fi

if ! sudo -u postgres psql -X -d "$DB_NAME" -Atqc \
  "SELECT 1" | grep -qx 1; then
  echo "ERROR: PostgreSQL database does not exist: $DB_NAME" >&2
  exit 66
fi

if sudo -u postgres psql -X -d postgres -Atqc \
  "SELECT 1 FROM pg_roles WHERE rolname = 'ubuntu'" | grep -qx 1; then
  echo "ERROR: PostgreSQL role '$ROLE_NAME' already exists; refusing to alter it" >&2
  exit 67
fi

cleanup() {
  status=$?
  trap - EXIT INT TERM HUP

  if [[ "$ROLE_CREATED" -eq 1 ]]; then
    sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
      --set=role_name="$ROLE_NAME" \
      --set=db_name="$DB_NAME" <<'SQL'
REVOKE pg_read_all_data FROM :"role_name";
REVOKE CONNECT ON DATABASE :"db_name" FROM :"role_name";
DROP ROLE :"role_name";
SQL
    echo "PASS: temporary PostgreSQL role '$ROLE_NAME' removed"
  fi

  exit "$status"
}
trap cleanup EXIT INT TERM HUP

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  --set=role_name="$ROLE_NAME" <<'SQL'
CREATE ROLE :"role_name" LOGIN;
SQL
ROLE_CREATED=1

sudo -u postgres psql -X -v ON_ERROR_STOP=1 -d postgres \
  --set=role_name="$ROLE_NAME" \
  --set=db_name="$DB_NAME" <<'SQL'
ALTER ROLE :"role_name" SET default_transaction_read_only = on;
GRANT CONNECT ON DATABASE :"db_name" TO :"role_name";
GRANT pg_read_all_data TO :"role_name";
SQL

if ! sudo -u postgres psql -X -d postgres -Atqc \
  "SELECT 1 FROM pg_roles WHERE rolname = 'ubuntu' AND rolcanlogin" \
  | grep -qx 1; then
  echo "ERROR: temporary role verification failed" >&2
  exit 68
fi

echo "PASS: temporary read-only PostgreSQL role '$ROLE_NAME' created"
echo "RUNNING: $*"

PGDATABASE="$DB_NAME" PGUSER="$ROLE_NAME" "$@"
