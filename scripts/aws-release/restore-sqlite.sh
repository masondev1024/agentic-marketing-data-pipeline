#!/usr/bin/env bash
# Promote only an identical, quiesced backup. Older backups need manual reconciliation.
set -euo pipefail
umask 077
stack="${1:?stack name required}"
bucket="${2:?stack bucket required}"
key="${3:?backup key required}"
[[ "$stack" =~ ^llm-ops-lab-[a-z0-9][a-z0-9-]{0,32}$ ]]
[[ "$bucket" == "$stack-backupbucket-"* ]]
[[ "$key" =~ ^backups/sqlite/[0-9]{8}T[0-9]{6}Z/investigations\.sqlite$ ]]
[[ "$(id -u)" == 0 ]]
if systemctl is-active --quiet llm-ops-lab-api.service; then
  echo 'stop_api_before_restore' >&2; exit 2
fi
target=/var/lib/llm-ops-lab/restored
[[ ! -e "$target" ]] || { echo 'restore_target_exists' >&2; exit 2; }
/usr/local/sbin/llm-ops-verify-sqlite-backup "$key"
install -d -m 0700 "$target"
aws s3 cp "s3://$bucket/$key" "$target/investigations.sqlite" --region eu-west-1 --only-show-errors
python3 - "$target" <<'PY'
import hashlib
import json
import sqlite3
import sys
from pathlib import Path

target = Path(sys.argv[1])
source = Path('/var/lib/llm-ops-lab/investigations.sqlite')
def state(path):
    with sqlite3.connect(f'file:{path}?mode=ro', uri=True) as db:
        assert db.execute('PRAGMA integrity_check').fetchone()[0] == 'ok'
        budget = db.execute('SELECT call_limit,calls_reserved FROM durable_budget').fetchall()
        dump = '\n'.join(db.iterdump()).encode()
        pending = db.execute("SELECT count(*) FROM durable_attempts WHERE status='pending'").fetchone()[0]
        return {'logical_sha256': hashlib.sha256(dump).hexdigest(),
                'budget': budget, 'pending_attempts': pending}
before, restored = state(source), state(target/'investigations.sqlite')
if before != restored:
    raise SystemExit('restore_not_identical_budget_reconciliation_required')
runner = Path('/usr/local/sbin/llm-ops-api-run')
command = runner.read_text()
old = '--durable-db /state/investigations.sqlite'
if command.count(old) != 1:
    raise SystemExit('unexpected_service_command')
runner.write_text(command.replace(old, '--durable-db /state/restored/investigations.sqlite'))
receipt = {'status':'passed', 'kind':'identical_quiesced_sqlite_restore',
           'source_equals_restored':True, 'budget_reconciled':True, **restored}
(target/'restore-receipt.json').write_text(json.dumps(receipt, sort_keys=True)+'\n')
print(json.dumps(receipt, sort_keys=True))
PY
chown -R 10001:10001 "$target"
chmod 0600 "$target/investigations.sqlite" "$target/restore-receipt.json"
echo 'restored_ledger_selected_api_remains_stopped=true'
