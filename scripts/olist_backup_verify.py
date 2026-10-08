"""Snapshot-consistent backup and restore into a NEW database on the local Olist container.

Does not overwrite/drop the product database, and retains the restored database as evidence.
"""

import hashlib
import json
import os
import subprocess
import uuid
from datetime import UTC, datetime
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo

from llm_ops_lab.olist.config import connection_info

ROOT = Path(__file__).resolve().parents[1]
COMPOSE = ['docker', 'compose', '-f', str(ROOT / 'compose.olist.yaml'),
           'exec', '-T', 'postgres']


def fingerprint(connection) -> dict:
    """Compare reproducible business state, not volatile connection statistics."""
    result = {}
    for schema, table in (
        ('olist_ops', 'versions'), ('olist_ops', 'publication'), ('olist_ops', 'runs'),
        ('olist_core', 'leads'), ('olist_core', 'sellers'), ('olist_core', 'deals'),
        ('olist_core', 'orders'), ('olist_core', 'items'),
        ('olist_mart', 'seller_funnel'), ('olist_mart', 'channel_funnel'),
    ):
        query = sql.SQL('SELECT row_to_json(t)::text FROM {}.{} t ORDER BY 1').format(
            sql.Identifier(schema), sql.Identifier(table)
        )
        digest = hashlib.sha256()
        count = 0
        with connection.cursor(name='backup_fingerprint') as cursor:
            cursor.execute(query)
            for row in cursor:
                digest.update(row[0].encode())
                digest.update(b'\n')
                count += 1
        result[f'{schema}.{table}'] = {'rows': count, 'sha256': digest.hexdigest()}
    return result


def execute(arguments, *, stdin=None, stdout=None):
    subprocess.run(COMPOSE + arguments, stdin=stdin, stdout=stdout,
                   stderr=subprocess.PIPE, timeout=180, check=True, cwd=ROOT)


def main() -> None:
    dsn = connection_info(ROOT)
    settings = conninfo_to_dict(dsn)
    if settings.get('host') != '127.0.0.1' or settings.get('dbname') != 'olist':
        raise ValueError('backup_verifier_is_local_only')
    if settings.get('port') != os.environ.get('OLIST_PG_PORT', '55432'):
        raise ValueError('backup_port_mismatch')
    token = uuid.uuid4().hex
    directory = ROOT / 'runs/olist/backups' / token
    directory.mkdir(parents=True)
    dump = directory / 'olist.dump'
    with psycopg.connect(dsn) as source:
        source.execute('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY')
        snapshot = source.execute('SELECT pg_export_snapshot()').fetchone()[0]
        before = fingerprint(source)
        with dump.open('xb') as output:
            execute(['pg_dump', '-U', 'postgres', '-d', 'olist', '-Fc',
                     '--snapshot', snapshot], stdout=output)
    restore_name = 'olist_restore_' + token
    execute(['createdb', '-U', 'postgres', '-O', 'olist_writer', restore_name],
            stdout=subprocess.DEVNULL)
    with dump.open('rb') as source:
        execute(['pg_restore', '-U', 'postgres', '-d', restore_name, '--exit-on-error'],
                stdin=source, stdout=subprocess.DEVNULL)
    with psycopg.connect(make_conninfo(dsn, dbname=restore_name)) as restored:
        after = fingerprint(restored)
    receipt = {
        'schema_version': 'olist-backup-restore.v1',
        'verified_at': datetime.now(UTC).isoformat(), 'operator': 'assistant',
        'status': 'passed' if before == after else 'failed',
        'dump_sha256': hashlib.sha256(dump.read_bytes()).hexdigest(),
        'restore_database': restore_name, 'source_preserved': True,
        'consistent_exported_snapshot': True, 'before': before, 'after': after,
    }
    (directory / 'receipt.json').write_text(json.dumps(receipt, indent=2) + '\n')
    print(json.dumps({'status': receipt['status'], 'receipt': str(directory / 'receipt.json'),
                      'restore_database': restore_name}))
    if before != after:
        raise RuntimeError('restore_fingerprint_mismatch')


if __name__ == '__main__':
    main()
