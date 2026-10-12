#!/usr/bin/env bash
set -euo pipefail
set +x
umask 077

usage() {
  cat <<'USAGE'
Usage: verify-postgres-recovery.sh --stack-name llm-ops-lab-<suffix> \
  --bucket <stack-owned-bucket> --parameter-prefix /llm-ops-lab/aws-release/<release-suffix> \
  --db-host <private-rds-endpoint>

Run on the stack-owned EC2 host while the API is stopped. It creates a custom-format
logical backup of `olist`, uploads and downloads it through the stack recovery bucket,
restores it to a new `olist_restore` database on the same RDS instance, and compares
row counts and deterministic row digests for olist_ops, olist_core, and olist_mart.
It refuses to replace an existing restore database.
USAGE
}

stack_name=''
bucket=''
parameter_prefix=''
db_host=''
while (($#)); do
  case "$1" in
    --stack-name) stack_name="${2:?missing stack name}"; shift 2 ;;
    --bucket) bucket="${2:?missing bucket}"; shift 2 ;;
    --parameter-prefix) parameter_prefix="${2:?missing parameter prefix}"; shift 2 ;;
    --db-host) db_host="${2:?missing database host}"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo 'unknown_argument' >&2; usage >&2; exit 2 ;;
  esac
done

[[ "$stack_name" =~ ^llm-ops-lab-[a-z0-9][a-z0-9-]{0,32}$ ]] || { echo 'invalid_stack_name' >&2; exit 2; }
[[ "$bucket" == "${stack_name}-backupbucket-"* ]] || { echo 'bucket_not_stack_scoped' >&2; exit 2; }
release_suffix="${stack_name#llm-ops-lab-}"
[[ "$parameter_prefix" == "/llm-ops-lab/aws-release/${release_suffix}" ]] || { echo 'parameter_prefix_not_stack_scoped' >&2; exit 2; }
[[ "$db_host" =~ ^[A-Za-z0-9.-]+$ && "$db_host" == *.eu-west-1.rds.amazonaws.com ]] || { echo 'invalid_private_rds_endpoint' >&2; exit 2; }
[[ "$(id -u)" == 0 ]] || { echo 'must_run_as_root_on_stack_instance' >&2; exit 2; }
if systemctl is-active --quiet llm-ops-lab-api.service; then
  echo 'api_service_must_be_stopped_for_recovery_verification' >&2
  exit 4
fi

region='eu-west-1'
parameter_name="$parameter_prefix/postgres-bootstrap"
if ! bootstrap_json="$(aws ssm get-parameter --region "$region" --name "$parameter_name" --with-decryption --query 'Parameter.Value' --output text 2>/dev/null)"; then
  echo 'bootstrap_parameter_unavailable' >&2
  exit 3
fi
username="$(jq -er '.username | select(type == "string" and length > 0 and test("^[A-Za-z0-9_]+$"))' <<<"$bootstrap_json")" || { echo 'bootstrap_parameter_invalid' >&2; exit 3; }
pg_password="$(jq -er '.password | select(type == "string" and length >= 16 and (contains("\n") | not) and (contains("\r") | not))' <<<"$bootstrap_json")" || { echo 'bootstrap_parameter_invalid' >&2; exit 3; }
unset bootstrap_json

work_dir="$(mktemp -d /var/tmp/llm-ops-postgres-recovery.XXXXXX)"
chmod 0700 "$work_dir"
cleanup() {
  unset pg_password username
  rm -rf "$work_dir"
}
trap cleanup EXIT

[[ -r /var/lib/llm-ops-lab/rds-ca.pem ]] || { echo 'rds_ca_bundle_missing' >&2; exit 3; }
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
ENV
chmod 0600 "$work_dir/postgres.env"
unset pg_password username

pg_exec() {
  docker run --rm -i --network host --env-file "$work_dir/postgres.env" \
    --mount type=bind,src=/var/lib/llm-ops-lab/rds-ca.pem,dst=/run/rds-ca.pem,readonly \
    --mount "type=bind,src=$work_dir,dst=/work" \
    "$pg_image" "$@"
}

# A uniquely named restore DB makes retries safe: an interrupted attempt requires
# explicit inspection and cleanup rather than silently overwriting prior evidence.
restore_exists="$(pg_exec psql --no-psqlrc --dbname=postgres --tuples-only --no-align -c \
  "SELECT count(*) FROM pg_database WHERE datname='olist_restore'")"
[[ "$restore_exists" == 0 ]] || { echo 'restore_database_already_exists_refusing_overwrite' >&2; exit 4; }

stamp="$(date -u +%Y%m%dT%H%M%SZ)"
backup_key="backups/postgres/$stamp/olist.dump"
source_manifest="$work_dir/source.manifest.tsv"
restore_manifest="$work_dir/restore.manifest.tsv"
receipt="$work_dir/receipt.json"

pg_exec pg_dump --no-owner --no-acl --format=custom --file=/work/olist.dump olist >/dev/null 2>&1 || {
  echo 'postgres_logical_dump_failed' >&2
  exit 5
}
sha256="$(sha256sum "$work_dir/olist.dump" | awk '{print $1}')"
# A single PutObject avoids multipart IAM permissions and keeps this bounded dump
# within S3's 5 GiB single-request limit.
aws s3api put-object --bucket "$bucket" --key "$backup_key" --body "$work_dir/olist.dump" \
  --region "$region" --server-side-encryption AES256 --query ETag --output text >/dev/null
aws s3 cp "s3://${bucket}/${backup_key}" "$work_dir/olist.downloaded.dump" --region "$region" --only-show-errors
restored_sha256="$(sha256sum "$work_dir/olist.downloaded.dump" | awk '{print $1}')"
[[ "$sha256" == "$restored_sha256" ]] || { echo 'postgres_backup_download_checksum_mismatch' >&2; exit 6; }

pg_exec psql --no-psqlrc --dbname=postgres --set ON_ERROR_STOP=1 \
  --command='CREATE DATABASE olist_restore TEMPLATE template0' >/dev/null
pg_exec pg_restore --no-owner --no-acl --exit-on-error --dbname=olist_restore /work/olist.downloaded.dump \
  >/dev/null 2>&1 || { echo 'postgres_restore_failed' >&2; exit 7; }

# The reader receives only database connect and mart schema/table SELECT rights.
pg_exec psql --no-psqlrc --dbname=postgres --set ON_ERROR_STOP=1 \
  --command='REVOKE CONNECT, TEMP ON DATABASE olist_restore FROM PUBLIC; GRANT CONNECT ON DATABASE olist_restore TO olist_reader' >/dev/null
pg_exec psql --no-psqlrc --dbname=olist_restore --set ON_ERROR_STOP=1 \
  --command='REVOKE ALL ON SCHEMA public FROM PUBLIC; GRANT USAGE ON SCHEMA olist_mart TO olist_reader; GRANT SELECT ON ALL TABLES IN SCHEMA olist_mart TO olist_reader; ALTER DEFAULT PRIVILEGES IN SCHEMA olist_mart GRANT SELECT ON TABLES TO olist_reader' >/dev/null

collect_manifest() {
  local database="$1"
  local schema table query result
  pg_exec psql --no-psqlrc --dbname="$database" --tuples-only --no-align --field-separator=$'\t' \
    --command="SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname IN ('olist_ops','olist_core','olist_mart') AND c.relkind IN ('r','p','v','m') ORDER BY n.nspname,c.relname" |
  while IFS=$'\t' read -r schema table; do
    [[ -n "$schema" && -n "$table" ]] || continue
    query="SELECT count(*)::text || E'\\t' || COALESCE(md5(string_agg(md5(row_to_json(t)::text), '' ORDER BY md5(row_to_json(t)::text))),md5('')) FROM :\"schema\".:\"table\" t"
    result="$(pg_exec psql --no-psqlrc --dbname="$database" --tuples-only --no-align \
      --set ON_ERROR_STOP=1 --set schema="$schema" --set table="$table" <<< "$query")"
    printf '%s\t%s\t%s\n' "$schema" "$table" "$result"
  done | LC_ALL=C sort
}

collect_manifest olist > "$source_manifest"
collect_manifest olist_restore > "$restore_manifest"
for schema in olist_ops olist_core olist_mart; do
  if ! awk -F '\t' -v wanted="$schema" '$1 == wanted {found=1} END {exit !found}' "$source_manifest"; then
    echo "source_schema_missing=$schema" >&2
    exit 8
  fi
done
if ! diff -q "$source_manifest" "$restore_manifest" >/dev/null; then
  echo 'postgres_rowcount_or_digest_mismatch' >&2
  exit 9
fi

python3 - "$receipt" "$stack_name" "$backup_key" "$sha256" "$source_manifest" <<'PY'
import json
from pathlib import Path
import sys

receipt_path, stack_name, backup_key, sha256, manifest_path = sys.argv[1:]
rows = []
for line in Path(manifest_path).read_text(encoding="utf-8").splitlines():
    schema, table, count, digest = line.split("\t")
    rows.append({"schema": schema, "table": table, "row_count": int(count), "row_digest_md5": digest})
Path(receipt_path).write_text(json.dumps({
    "schema": "llm-ops-lab-postgres-recovery.v1",
    "status": "passed",
    "stack": stack_name,
    "backup_key": backup_key,
    "dump_sha256": sha256,
    "comparison": "row_count_and_sorted_row_md5_digest",
    "relation_count": len(rows),
    "relations": rows,
}, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
PY
chmod 0600 "$receipt"
aws s3 cp "$receipt" "s3://${bucket}/evidence/postgres/$stamp/receipt.json" \
  --region "$region" --sse AES256 --only-show-errors
relation_count="$(wc -l < "$source_manifest" | tr -d ' ')"
echo "postgres_recovery=passed backup_key=$backup_key dump_sha256=$sha256 relation_count=$relation_count restore_database=olist_restore comparison=matched"
