"""Real PostgreSQL boundaries with scripted actions; no model-quality claims."""

import json
import time

import pytest
from test_olist_postgres import ADMIN
from test_olist_postgres import database as database
from test_olist_validation import _make_fixture

from llm_ops_lab.olist.agent import investigate
from llm_ops_lab.olist.pipeline import run_pipeline
from llm_ops_lab.olist.tool_session import OlistToolSession, SessionError

pytestmark = pytest.mark.skipif(not ADMIN, reason='dedicated Olist PostgreSQL not enabled')


def test_agent_reads_real_mart_only_when_tool_selected(tmp_path, database):
    writer, reader = database
    intake = _make_fixture(tmp_path)
    published = run_pipeline(tmp_path, writer, intake_path=intake)
    actions = iter([
        {'action': 'tool', 'tool': 'channel_funnel', 'parameters': {}},
        {'action': 'finish', 'status': 'completed',
         'finding_codes': ['seller_sample_coverage_unknown'],
         'evidence_ids': ['e2'], 'limitation_codes': []},
    ])

    def generate(messages, timeout):
        assert timeout > 0
        return {'output': json.dumps(next(actions)), 'prompt_tokens': 100,
                'generation_tokens': 30}

    outcome = investigate(reader, '판매자 표본 연결 상태는?', generate)
    assert outcome['status'] == 'completed'
    assert outcome['dataset_version'] == published['version_id']
    assert [e['tool'] for e in outcome['evidence']] == ['version_evidence', 'channel_funnel']
    assert 'unknown, not zero' in outcome['findings'][0]['summary']
    assert outcome['usage']['tool_calls'] == 1


def test_agent_snapshot_does_not_mix_a_new_publication(tmp_path, database):
    writer, reader = database
    first_root = tmp_path / 'first'
    first = run_pipeline(first_root, writer, intake_path=_make_fixture(first_root))
    with OlistToolSession(reader, version_id=None, deadline=time.monotonic() + 30) as session:
        before = session.initialize()
        second_root = tmp_path / 'second'
        second = run_pipeline(second_root, writer,
                              intake_path=_make_fixture(second_root, price='12.00'))
        assert second['status'] == 'published'
        assert second['version_id'] != first['version_id']
        assert session.initialize() == before
        assert session.call('channel_funnel')['dataset_version'] == first['version_id']
        assert before['data']['active_published_version_id'] == first['version_id']
        assert all(row['version_id'] != second['version_id']
                   for row in session.call('run_history')['data'])


def test_privileged_writer_connection_is_rejected_before_any_model_call(database):
    writer, _ = database
    with pytest.raises(SessionError, match='reader_role_required'):
        with OlistToolSession(writer, version_id=None, deadline=time.monotonic() + 5):
            pytest.fail('writer must not be an investigation principal')
