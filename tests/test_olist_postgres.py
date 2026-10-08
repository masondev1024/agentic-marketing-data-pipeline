"""Opt-in real PostgreSQL integration, isolated databases on the project's local container.

OLIST_TEST_ADMIN_DSN must point to the dedicated local PostgreSQL; no cloud test targets.
"""

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from pathlib import Path

import psycopg
import pytest
from psycopg import sql
from psycopg.conninfo import conninfo_to_dict, make_conninfo
from test_olist_validation import _make_fixture, _rewrite_csv

from llm_ops_lab.olist.pipeline import LOCK_ID, migrate, run_pipeline

ROOT = Path(__file__).resolve().parents[1]
ADMIN = os.environ.get('OLIST_TEST_ADMIN_DSN')
pytestmark = pytest.mark.skipif(not ADMIN, reason='dedicated Olist PostgreSQL not enabled')


@pytest.fixture
def database():
    parameters = conninfo_to_dict(ADMIN)
    assert parameters['host'] == '127.0.0.1'
    assert parameters['port'] == '55432'
    name = 'olist_test_' + uuid.uuid4().hex
    with psycopg.connect(ADMIN, autocommit=True) as admin:
        admin.execute(sql.SQL('CREATE DATABASE {} OWNER olist_writer').format(sql.Identifier(name)))
    secret_root = ROOT / 'work/olist/secrets'
    writer = make_conninfo(ADMIN, dbname=name, user='olist_writer',
                          password=(secret_root / 'writer.txt').read_text().strip())
    reader = make_conninfo(ADMIN, dbname=name, user='olist_reader',
                          password=(secret_root / 'reader.txt').read_text().strip())
    migrate(writer)
    try:
        yield writer, reader
    finally:
        # Only this test's fresh UUID database; never the user's product database or volume.
        with psycopg.connect(ADMIN, autocommit=True) as admin:
            admin.execute(sql.SQL('DROP DATABASE {}').format(sql.Identifier(name)))


def current(dsn):
    with psycopg.connect(dsn) as connection:
        row = connection.execute('SELECT version_id FROM olist_mart.current_version').fetchone()
        return row[0] if row else None


def test_duplicate_delivery_does_not_duplicate_rows_or_zero_unknown(tmp_path, database):
    writer, reader = database
    intake = _make_fixture(tmp_path)
    first = run_pipeline(tmp_path, writer, intake_path=intake)
    second = run_pipeline(tmp_path, writer, intake_path=intake)
    assert first['status'] == 'published'
    assert second['status'] == 'reused'
    assert first['version_id'] == second['version_id'] == current(reader)
    with psycopg.connect(reader) as connection:
        row = connection.execute(
            'SELECT coverage_state,observed_order_count FROM olist_mart.seller_funnel'
        ).fetchone()
        assert row == ('unlinked_unknown', None)
        rows = connection.execute('SELECT count(*) FROM olist_mart.seller_funnel').fetchone()
        assert rows == (1,)


@pytest.mark.parametrize('failpoint', ['after_load', 'before_publish'])
def test_partial_failure_preserves_last_good_version_and_records_failure(
    tmp_path, database, failpoint
):
    writer, reader = database
    good_intake = _make_fixture(tmp_path / 'good')
    good = run_pipeline(tmp_path / 'good', writer, intake_path=good_intake)
    next_intake = _make_fixture(tmp_path / 'next', price='12.30')
    failed = run_pipeline(tmp_path / 'next', writer, intake_path=next_intake, fail_at=failpoint)
    assert failed['status'] == 'failed'
    assert current(reader) == good['version_id']
    with psycopg.connect(writer) as connection:
        assert connection.execute('SELECT count(*) FROM olist_ops.versions').fetchone() == (1,)
        assert connection.execute(
            "SELECT status FROM olist_ops.runs WHERE run_id=%s", (failed['run_id'],)
        ).fetchone() == ('failed',)
        assert connection.execute('SELECT count(*) FROM olist_core.items').fetchone() == (1,)
    recovered = run_pipeline(tmp_path / 'next', writer, intake_path=next_intake)
    assert recovered['status'] == 'published'
    assert current(reader) == recovered['version_id']


def test_bad_source_does_not_publish(tmp_path, database):
    writer, reader = database
    intake = _make_fixture(tmp_path, price='NaN')
    result = run_pipeline(tmp_path, writer, intake_path=intake)
    assert result['error_code'] == 'source_contract_failed'
    assert current(reader) is None


def test_reader_cannot_access_raw_tables_or_write_even_if_readonly_is_disabled(database):
    writer, reader = database
    with psycopg.connect(reader, autocommit=True) as connection:
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('SELECT * FROM olist_core.orders')
        connection.execute('SET default_transaction_read_only=off')
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            connection.execute('DELETE FROM olist_mart.seller_funnel')


def test_concurrent_publisher_rejected_and_orphan_run_recovered(tmp_path, database):
    writer, reader = database
    intake = _make_fixture(tmp_path)
    with psycopg.connect(writer, autocommit=True) as owner:
        owner.execute('SELECT pg_advisory_lock(%s)', (LOCK_ID,))
        assert run_pipeline(tmp_path, writer, intake_path=intake)['status'] == 'busy'
        run_id = str(uuid.uuid4())
        owner.execute("INSERT INTO olist_ops.runs(run_id,status,step) VALUES (%s,'running','load')",
                      (run_id,))
    assert run_pipeline(tmp_path, writer, intake_path=intake)['status'] == 'published'
    with psycopg.connect(reader) as connection:
        assert connection.execute(
            'SELECT status,error_code FROM olist_mart.run_history WHERE run_id=%s', (run_id,)
        ).fetchone() == ('abandoned', 'publisher_interrupted')


def test_diagnosis_reads_pinned_evidence_and_handles_empty_database(tmp_path, database):
    from llm_ops_lab.olist.diagnostics import diagnose
    writer, reader = database
    assert diagnose(reader)['status'] == 'abstained'
    intake = _make_fixture(tmp_path)
    published = run_pipeline(tmp_path, writer, intake_path=intake)
    result = diagnose(reader)
    assert result['status'] == 'completed'
    assert result['dataset_version'] == published['version_id']
    assert result['model_executed'] is False
    assert result['evidence']


def test_migration_checksum_drift_fails_without_schema_changes(database):
    from llm_ops_lab.olist.pipeline import PipelineFailure
    writer, _reader = database
    with psycopg.connect(writer) as connection:
        connection.execute("UPDATE olist_ops.migrations SET sha256='tampered'")
    with pytest.raises(PipelineFailure, match='migration_checksum_changed'):
        migrate(writer)


def test_replay_from_retained_bronze_after_original_intake_is_unavailable(tmp_path, database):
    writer, _reader = database
    intake = _make_fixture(tmp_path)
    original = run_pipeline(tmp_path, writer, intake_path=intake)
    shutil.move(str(tmp_path / 'work/olist-intake'), str(tmp_path / 'original-removed'))
    replay = run_pipeline(tmp_path, writer, intake_path=Path(original['replay_intake']))
    assert replay['status'] == 'reused'
    assert replay['version_id'] == original['version_id']


def test_grain_and_source_clock_comparison_do_not_count_items_as_orders(tmp_path, database):
    writer, reader = database
    intake = _make_fixture(tmp_path)
    _rewrite_csv(tmp_path, intake, 'olist_closed_deals_dataset.csv',
                 lambda _cols, rows: [{**rows[0], 'seller_id': 'seller-1'}])
    _rewrite_csv(tmp_path, intake, 'olist_orders_dataset.csv',
                 lambda _cols, rows: [rows[0], {**rows[0], 'order_id': 'order-before',
                                               'order_purchase_timestamp': '2018-01-01 00:00:00'}])
    _rewrite_csv(tmp_path, intake, 'olist_order_items_dataset.csv',
                 lambda _cols, rows: [rows[0], {**rows[0], 'order_item_id': '2'},
                                      {**rows[0], 'order_id': 'order-before'}])
    # A new fixture source must keep extracted files and archive members in agreement.
    metadata = json.loads(intake.read_text())
    for archive in metadata['datasets']:
        path = tmp_path / archive['local_path']
        with zipfile.ZipFile(path, 'w') as zipped:
            for item in metadata['profile']['files']:
                if archive['dataset'].split('/')[-1] in item['local_path']:
                    zipped.write(tmp_path / item['local_path'], arcname=item['file'])
        archive.update(bytes=path.stat().st_size,
                       sha256=hashlib.sha256(path.read_bytes()).hexdigest())
    intake.write_text(json.dumps(metadata))
    result = run_pipeline(tmp_path, writer, intake_path=intake)
    assert result['status'] == 'published', result
    with psycopg.connect(reader) as connection:
        row = connection.execute(
            'SELECT observed_order_count,post_contract_order_count_source_clock,'
            'pre_contract_order_count_source_clock,clock_basis FROM olist_mart.seller_funnel'
        ).fetchone()
        assert row == (2, 1, 1, 'naive_source_clock_unverified')
