"""Single-publisher PostgreSQL load with immutable versions and last-good publication."""

from __future__ import annotations

import csv
import hashlib
import json
import os
import shutil
import uuid
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path

import psycopg
from psycopg import sql
from psycopg.types.json import Jsonb

from .validation import TABLE_FILES, file_sha256, validate_sources

PACKAGE = Path(__file__).parent
LOCK_ID = 481503217


class PipelineFailure(Exception):
    """Stable public failure code without raw rows or connection strings."""


def migrate(dsn: str) -> None:
    """Apply the initial schema atomically; changed migrations require a new version."""
    source = (PACKAGE / 'schema.sql').read_text()
    digest = hashlib.sha256(source.encode()).hexdigest()
    with psycopg.connect(dsn) as connection:
        connection.execute('SELECT pg_advisory_xact_lock(%s)', (LOCK_ID,))
        connection.execute(source)
        prior = connection.execute(
            "SELECT sha256 FROM olist_ops.migrations WHERE name='schema-v1'"
        ).fetchone()
        if prior and prior[0] != digest:
            raise PipelineFailure('migration_checksum_changed')
        connection.execute(
            "INSERT INTO olist_ops.migrations(name,sha256) VALUES ('schema-v1',%s) "
            'ON CONFLICT DO NOTHING', (digest,),
        )


def transform_hash() -> str:
    """Bind a release to both SQL and loader implementation, not a mutable name."""
    digest = hashlib.sha256()
    for name in ('schema.sql', 'transform.sql', 'pipeline.py', 'validation.py'):
        digest.update(name.encode())
        digest.update((PACKAGE / name).read_bytes())
    return digest.hexdigest()


def _freeze_sources(root: Path, report: dict, intake_path: Path | None) -> tuple[dict, Path]:
    """Retain the complete validated intake, including a replayable manifest."""
    directory = root / 'work/olist/bronze' / report['dataset_id']
    directory.mkdir(parents=True, exist_ok=True)
    metadata_path = intake_path or root / 'data/olist/intake-v1.json'
    if not metadata_path.is_absolute():
        metadata_path = root / metadata_path
    metadata = json.loads(metadata_path.read_text())
    validated = {item['file']: item['sha256'] for item in report['files']}
    archive_hashes = {check['rule_id']: check['observed'] for check in report['checks']
                      if check['rule_id'].endswith('.archive_sha256')}
    by_file = {}
    for item in [*metadata['datasets'], *metadata['profile']['files']]:
        original = root / item['local_path']
        relative = ('raw/' + item['file']) if 'file' in item else ('archives/' + original.name)
        target = directory / relative
        target.parent.mkdir(exist_ok=True)
        if 'file' in item:
            expected = validated[item['file']]
        else:
            archive_name = item['dataset'].split('/')[-1]
            expected = archive_hashes[f'{archive_name}.archive_sha256']
        if not target.exists():
            temporary = target.with_suffix(f'.{uuid.uuid4().hex}.tmp')
            try:
                with original.open('rb') as source, temporary.open('xb') as destination:
                    shutil.copyfileobj(source, destination, 1024 * 1024)
                    destination.flush()
                    os.fsync(destination.fileno())
                if file_sha256(temporary) != expected:
                    raise PipelineFailure('source_changed_after_validation')
                os.replace(temporary, target)
                target.chmod(0o444)
            finally:
                temporary.unlink(missing_ok=True)
        if file_sha256(target) != expected:
            raise PipelineFailure('bronze_hash_mismatch')
        item['local_path'] = str(target.relative_to(root))
        if 'file' in item:
            by_file[item['file']] = target
        parent_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(parent_fd)
        finally:
            os.close(parent_fd)
    replay = directory / 'intake.json'
    content = json.dumps(metadata, sort_keys=True, ensure_ascii=False, indent=2) + '\n'
    # Metadata annotations/path of the original fetch can differ on a replay;
    # source hash identity, verified above, is the immutable identity.
    if not replay.exists():
        temporary = replay.with_suffix(f'.{uuid.uuid4().hex}.tmp')
        try:
            with temporary.open('x') as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, replay)
            replay.chmod(0o444)
        finally:
            temporary.unlink(missing_ok=True)
    else:
        prior = json.loads(replay.read_text())
        if prior['datasets'] != metadata['datasets'] or prior['profile'] != metadata['profile']:
            raise PipelineFailure('bronze_manifest_mismatch')
    directory_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)
    sources = {table: by_file[Path(relative).name] for table, relative in TABLE_FILES.items()}
    return sources, replay


def _record_step(connection, run_id: str, step: str) -> None:
    connection.execute(
        'UPDATE olist_ops.runs SET step=%s WHERE run_id=%s', (step, run_id)
    )


def _normalize(table: str, row: dict) -> tuple:
    """Only contractual fields are promoted; money retains unspecified source currency."""
    if table == 'leads':
        return (row['mql_id'], date.fromisoformat(row['first_contact_date']),
                row['origin'].strip() or None)
    if table == 'sellers':
        return (row['seller_id'],)
    if table == 'deals':
        return row['mql_id'], row['seller_id'], datetime.fromisoformat(row['won_date'])
    if table == 'orders':
        return (row['order_id'], row['order_status'],
                datetime.fromisoformat(row['order_purchase_timestamp']))
    return (row['order_id'], int(row['order_item_id']), row['seller_id'],
            Decimal(row['price']), Decimal(row['freight_value']))


def _load(connection, version: str, sources: dict) -> tuple[dict, dict]:
    counts = {}
    totals = {'price_source': Decimal(0), 'freight_source': Decimal(0)}
    for table in ('leads', 'sellers', 'deals', 'orders', 'items'):
        count = 0
        query = sql.SQL('COPY olist_core.{} FROM STDIN').format(sql.Identifier(table))
        with sources[table].open(encoding='utf-8-sig', newline='') as stream:
            with connection.cursor().copy(query) as copy:
                for row in csv.DictReader(stream):
                    copy.write_row((version, *_normalize(table, row)))
                    count += 1
                    if table == 'items':
                        totals['price_source'] += Decimal(row['price'])
                        totals['freight_source'] += Decimal(row['freight_value'])
        counts[table] = count
    return counts, totals


def _reconcile(connection, version: str, counts: dict, report: dict, totals: dict) -> list[dict]:
    expected = {item['file']: item['rows'] for item in report['files']}
    checks = []
    for table, count in counts.items():
        target = expected[Path(TABLE_FILES[table]).name]
        query = sql.SQL('SELECT count(*) FROM olist_core.{} WHERE version_id=%s').format(
            sql.Identifier(table)
        )
        stored = connection.execute(query, (version,)).fetchone()[0]
        if count != target or stored != target:
            raise PipelineFailure('row_reconciliation_failed')
        checks.append({'rule_id': f'loaded_rows.{table}', 'observed': stored,
                       'expected': target, 'status': 'passed', 'severity': 'error'})
    stored_totals = connection.execute(
        'SELECT coalesce(sum(price_source),0),coalesce(sum(freight_source),0) '
        'FROM olist_core.items WHERE version_id=%s', (version,),
    ).fetchone()
    for field, observed in zip(('price_source', 'freight_source'), stored_totals, strict=True):
        if observed != totals[field]:
            raise PipelineFailure('source_decimal_reconciliation_failed')
        checks.append({'rule_id': f'source_decimal_reconciliation.{field}',
                       'observed': str(observed), 'expected': str(totals[field]),
                       'status': 'passed', 'severity': 'error'})
    actual = connection.execute(
        'SELECT sum(lead_count),sum(contract_count),sum(unlinked_contract_count) '
        'FROM olist_mart.channel_funnel WHERE version_id=%s', (version,),
    ).fetchone()
    seller_count, unlinked = connection.execute(
        "SELECT count(*),count(*) FILTER (WHERE coverage_state='unlinked_unknown') "
        'FROM olist_mart.seller_funnel WHERE version_id=%s', (version,),
    ).fetchone()
    if (actual[0] or 0) != counts['leads'] or (actual[1] or 0) != counts['deals']:
        raise PipelineFailure('channel_fanout_or_loss')
    if seller_count != counts['deals'] or (actual[2] or 0) != unlinked:
        raise PipelineFailure('seller_fanout_or_loss')
    checks.append({'rule_id': 'mart_grain_reconciliation', 'status': 'passed',
                   'severity': 'error', 'observed': seller_count, 'expected': counts['deals']})
    return checks


def run_pipeline(root: Path, dsn: str, *, intake_path: Path | None = None,
                 fail_at: str | None = None) -> dict:
    """Publish after all gates. Test-only failpoints prove rollback, never alter source files."""
    if fail_at not in (None, 'after_load', 'before_publish'):
        raise ValueError('unsupported_failpoint')
    run_id = str(uuid.uuid4())
    version = None
    report = None
    # Session lock spans validation and publication. It is automatically released on crash.
    with psycopg.connect(dsn, autocommit=True) as connection:
        acquired = connection.execute('SELECT pg_try_advisory_lock(%s)', (LOCK_ID,)).fetchone()[0]
        if not acquired:
            return {'status': 'busy', 'error_code': 'publisher_busy', 'run_id': None}
        with psycopg.connect(dsn, autocommit=True) as journal:
            # Exclusive lock proves no prior publisher still owns a running attempt.
            journal.execute(
                "UPDATE olist_ops.runs SET status='abandoned',error_code='publisher_interrupted',"
                "finished_at=now() WHERE status='running'"
            )
            journal.execute(
                "INSERT INTO olist_ops.runs(run_id,status,step) VALUES (%s,'running','validate')",
                (run_id,),
            )
            try:
                report = validate_sources(root, intake_path)
                journal.execute('UPDATE olist_ops.runs SET validation=%s WHERE run_id=%s',
                                (Jsonb(report), run_id))
                if report['status'] != 'passed':
                    raise PipelineFailure('source_contract_failed')
                transform = transform_hash()
                version = hashlib.sha256(
                    f"{report['dataset_id']}:{transform}".encode()
                ).hexdigest()
                journal.execute('UPDATE olist_ops.runs SET version_id=%s WHERE run_id=%s',
                                (version, run_id))
                _record_step(journal, run_id, 'freeze_sources')
                sources, replay = _freeze_sources(root, report, intake_path)
                with connection.transaction():
                    exists = connection.execute(
                        'SELECT 1 FROM olist_ops.versions WHERE version_id=%s', (version,)
                    ).fetchone()
                    if exists:
                        status = 'reused'
                    else:
                        _record_step(journal, run_id, 'load')
                        manifest = [{'table': name, 'sha256': file_sha256(path),
                                     'path': str(path.relative_to(root))}
                                    for name, path in sources.items()]
                        connection.execute(
                            'INSERT INTO olist_ops.versions(version_id,dataset_id,contract_version,'
                            'transform_sha256,source_manifest,quality_report) '
                            'VALUES (%s,%s,%s,%s,%s,%s)',
                            (version, report['dataset_id'], report['contract_version'], transform,
                             Jsonb(manifest), Jsonb(report)),
                        )
                        counts, totals = _load(connection, version, sources)
                        if fail_at == 'after_load':
                            raise PipelineFailure('injected_after_load')
                        # A new immutable version is absent from old value-frequency statistics.
                        # Refresh before planning joins; otherwise a large new slice can be
                        # estimated as one row and trigger quadratic nested-loop work.
                        _record_step(journal, run_id, 'analyze')
                        for table in counts:
                            connection.execute(sql.SQL('ANALYZE olist_core.{}').format(
                                sql.Identifier(table)
                            ))
                        _record_step(journal, run_id, 'mart')
                        for statement in (PACKAGE / 'transform.sql').read_text().split(';'):
                            if statement.strip():
                                connection.execute(statement, {'version': version})
                        report['checks'].extend(
                            _reconcile(connection, version, counts, report, totals)
                        )
                        for item in manifest:
                            if file_sha256(root / item['path']) != item['sha256']:
                                raise PipelineFailure('bronze_changed_during_load')
                        connection.execute(
                            'UPDATE olist_ops.versions SET quality_report=%s WHERE version_id=%s',
                            (Jsonb(report), version),
                        )
                        status = 'published'
                    _record_step(journal, run_id, 'publish')
                    if fail_at == 'before_publish':
                        raise PipelineFailure('injected_before_publish')
                    connection.execute(
                        "INSERT INTO olist_ops.publication(product,version_id) VALUES ('olist',%s) "
                        'ON CONFLICT (product) DO UPDATE SET version_id=excluded.version_id',
                        (version,),
                    )
                    # Completion receipt shares the data commit, avoiding an ambiguous success log.
                    connection.execute(
                        "UPDATE olist_ops.runs SET status=%s,step='complete',finished_at=now() "
                        'WHERE run_id=%s', (status, run_id),
                    )
                return {'status': status, 'run_id': run_id, 'version_id': version,
                        'dataset_id': report['dataset_id'], 'model_executed': False,
                        'replay_intake': str(replay.relative_to(root))}
            except Exception as exc:
                code = str(exc) if isinstance(exc, PipelineFailure) else type(exc).__name__
                # A lost COMMIT acknowledgement must not overwrite a committed success receipt.
                journal.execute(
                    "UPDATE olist_ops.runs SET status='failed',error_code=%s,finished_at=now() "
                    "WHERE run_id=%s AND status='running'", (code, run_id),
                )
                receipt = journal.execute(
                    'SELECT status FROM olist_ops.runs WHERE run_id=%s', (run_id,)
                ).fetchone()[0]
                if receipt in ('published', 'reused'):
                    return {'status': receipt, 'run_id': run_id, 'version_id': version,
                            'commit_ack_recovered': True, 'model_executed': False}
                return {'status': 'failed', 'run_id': run_id, 'version_id': version,
                        'error_code': code, 'model_executed': False}


def save_report(path: Path, result: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
