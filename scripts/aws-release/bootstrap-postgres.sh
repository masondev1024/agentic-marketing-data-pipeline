#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

usage() {
  cat <<'USAGE'
Usage: bootstrap-postgres.sh --stack-name llm-ops-lab-<suffix> --bucket <stack-owned-bucket> \
  --parameter-prefix /llm-ops-lab/aws-release/<stack-name> --db-host <private-rds-endpoint> \
  --dump-sha256 <64-hex-sha256>

Runs on the stack-owned EC2 instance via SSM. Restores the verified custom-format
Olist dump into an empty private PostgreSQL database and creates a mart-only reader.
It expects <parameter-prefix>/postgres-bootstrap to contain temporary JSON fields
username, password, and reader_password. The caller must delete that parameter after
this command. The script never prints secret values.
USAGE
}

stack_name=''
bucket=''
parameter_prefix=''
db_host=''
dump_sha256=''
while (($#)); do
  case "$1" in
    --stack-name) stack_name="${2:?missing stack name}"; shift 2 ;;
    --bucket) bucket="${2:?missing bucket}"; shift 2 ;;
    --parameter-prefix) parameter_prefix="${2:?missing parameter prefix}"; shift 2 ;;
    --db-host) db_host="${2:?missing database host}"; shift 2 ;;
    --dump-sha256) dump_sha256="${2:?missing dump checksum}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo 'unknown_argument' >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$stack_name" =~ ^llm-ops-lab-[a-z0-9][a-z0-9-]{0,32}$ ]] || { echo 'invalid_stack_name' >&2; exit 2; }
[[ "$bucket" == "${stack_name}-backupbucket-"* ]] || { echo 'bucket_not_stack_scoped' >&2; exit 2; }
release_suffix="${stack_name#llm-ops-lab-}"
[[ "$parameter_prefix" == "/llm-ops-lab/aws-release/${release_suffix}" ]] || { echo 'parameter_prefix_not_stack_scoped' >&2; exit 2; }
[[ "$db_host" =~ ^[A-Za-z0-9.-]+$ && "$db_host" == *.eu-west-1.rds.amazonaws.com ]] || { echo 'invalid_private_rds_endpoint' >&2; exit 2; }
[[ "$dump_sha256" =~ ^[a-fA-F0-9]{64}$ ]] || { echo 'invalid_dump_sha256' >&2; exit 2; }

work_dir="$(mktemp -d /var/tmp/llm-ops-postgres-bootstrap.XXXXXX)"
chmod 0700 "$work_dir"
cleanup() {
  unset bootstrap_json pg_password reader_password
  rm -rf "$work_dir"
}
trap cleanup EXIT

region='eu-west-1'
parameter_name="$parameter_prefix/postgres-bootstrap"
if ! bootstrap_json="$(aws ssm get-parameter --region "$region" --name "$parameter_name" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null)"; then
  echo 'bootstrap_parameter_unavailable' >&2
  exit 3
fi
username="$(jq -er '.username | select(type == "string" and length > 0 and (test("^[A-Za-z0-9_]+$")))' <<<"$bootstrap_json")" || { echo 'bootstrap_parameter_invalid' >&2; exit 3; }
pg_password="$(jq -er '.password | select(type == "string" and length >= 16 and (contains("\n") | not) and (contains("\r") | not))' <<<"$bootstrap_json")" || { echo 'bootstrap_parameter_invalid' >&2; exit 3; }
reader_password="$(jq -er '.reader_password | select(type == "string" and length >= 24 and (contains("\n") | not) and (contains("\r") | not))' <<<"$bootstrap_json")" || { echo 'bootstrap_parameter_invalid' >&2; exit 3; }
unset bootstrap_json

install -d -m 0750 /var/lib/llm-ops-lab
curl --fail --silent --show-error --location \
  https://truststore.pki.rds.amazonaws.com/global/global-bundle.pem \
  --output "$work_dir/rds-ca.pem"
chmod 0644 "$work_dir/rds-ca.pem"
install -m 0644 "$work_dir/rds-ca.pem" /var/lib/llm-ops-lab/rds-ca.pem
aws s3 cp "s3://${bucket}/backups/seed/olist.dump" "$work_dir/olist.dump" --region "$region" --only-show-errors
actual_sha256="$(sha256sum "$work_dir/olist.dump" | awk '{print $1}')"
expected_sha256="$(printf '%s' "$dump_sha256" | tr '[:upper:]' '[:lower:]')"
[[ "$actual_sha256" == "$expected_sha256" ]] || { echo 'seed_dump_checksum_mismatch' >&2; exit 4; }

pg_image='postgres:17.11-bookworm@sha256:3645570cccdfa447589da9f57dd740faa29b30938e861289a5574b6ca6b03826'
docker pull "$pg_image" >/dev/null
cat > "$work_dir/postgres.env" <<ENV
PGHOST=${db_host}
PGPORT=5432
PGDATABASE=olist
PGUSER=${username}
PGPASSWORD=${pg_password}
PGSSLMODE=verify-full
PGSSLROOTCERT=/run/rds-ca.pem
READER_ROLE_PASSWORD=${reader_password}
ENV
chmod 0600 "$work_dir/postgres.env"
READER_ROLE_PASSWORD="$reader_password" python3 - "$work_dir/reader-role.sql" <<'PY'
import os
from pathlib import Path
import sys

password = os.environ["READER_ROLE_PASSWORD"]
literal = "'" + password.replace("'", "''") + "'"
Path(sys.argv[1]).write_text(
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'olist_reader') "
    "THEN RAISE EXCEPTION 'olist_reader_role_already_exists'; END IF; END $$;\n"
    f"CREATE ROLE olist_reader LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE PASSWORD {literal};\n"
    "ALTER ROLE olist_reader SET default_transaction_read_only = on;\n"
    "ALTER ROLE olist_reader SET statement_timeout = '5s';\n"
    "REVOKE ALL PRIVILEGES ON DATABASE olist FROM PUBLIC;\n"
    "REVOKE ALL ON SCHEMA public FROM PUBLIC;\n"
    "GRANT CONNECT ON DATABASE olist TO olist_reader;\n"
    "GRANT USAGE ON SCHEMA olist_mart TO olist_reader;\n"
    "GRANT SELECT ON ALL TABLES IN SCHEMA olist_mart TO olist_reader;\n"
    "ALTER DEFAULT PRIVILEGES IN SCHEMA olist_mart GRANT SELECT ON TABLES TO olist_reader;\n",
    encoding="utf-8",
)
PY
chmod 0600 "$work_dir/reader-role.sql"
unset pg_password reader_password username READER_ROLE_PASSWORD

pg_exec() {
  docker run --rm --network host --env-file "$work_dir/postgres.env" \
    --mount "type=bind,src=$work_dir/rds-ca.pem,dst=/run/rds-ca.pem,readonly" \
    --mount "type=bind,src=$work_dir/olist.dump,dst=/run/olist.dump,readonly" \
    --mount "type=bind,src=$work_dir/reader-role.sql,dst=/run/reader-role.sql,readonly" \
    "$pg_image" "$@"
}

# Do not load into a nonempty target database. This makes retry behavior explicit:
# an interrupted restore must be inspected and the dedicated DB recreated first.
existing_objects="$(pg_exec psql --no-psqlrc --tuples-only --no-align -c \
  "SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname NOT IN ('pg_catalog','information_schema') AND c.relkind IN ('r','v','m','f','p','S')")"
if [[ "$existing_objects" != 0 ]]; then
  echo 'target_database_not_empty_refusing_restore' >&2
  exit 5
fi
pg_exec pg_restore --no-owner --no-acl --verbose --exit-on-error --dbname=olist /run/olist.dump \
  >/dev/null 2>&1
pg_exec psql --no-psqlrc --set ON_ERROR_STOP=1 --file=/run/reader-role.sql >/dev/null
rm -f "$work_dir/postgres.env" "$work_dir/reader-role.sql"
echo 'postgres_bootstrap=passed dump_sha256_verified=true reader_role=olist_reader schema_scope=olist_mart'
