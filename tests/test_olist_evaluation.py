import pytest

from llm_ops_lab.olist.evaluation import score_case


def result():
    return {
        'model_executed': True, 'dataset_version': 'v1', 'status': 'completed',
        'evidence': [{'evidence_id': 'e1', 'tool': 'channel_funnel'}],
        'findings': [{'code': 'missing_origin_leads', 'evidence_ids': ['e1']}],
    }


def test_correct_code_with_fabricated_reference_or_no_inference_fails():
    case = {'required_codes': ['missing_origin_leads']}
    baseline = {'dataset_version': 'v1', 'findings': []}
    assert score_case(case, result(), baseline)['passed']
    fabricated = result()
    fabricated['findings'][0]['evidence_ids'] = ['not-observed']
    assert not score_case(case, fabricated, baseline)['passed']
    mocked = result() | {'model_executed': False}
    assert not score_case(case, mocked, baseline)['passed']


def test_unrelated_safe_refusal_is_not_scored_as_correct_metric_refusal():
    case = {'expected_refusal': 'unsupported_metric'}
    baseline = {'dataset_version': 'v1', 'findings': []}
    refused = result() | {'status': 'abstained', 'findings': [],
                         'limitations': ['invalid_action']}
    assert not score_case(case, refused, baseline)['passed']
    refused['limitations'] = ['unsupported_metric']
    assert not score_case(case, refused, baseline)['passed']
    refused['stop_reason'] = 'model_abstained'
    refused['evidence'][0]['tool'] = 'definitions'
    assert score_case(case, refused, baseline)['passed']


def test_pipeline_case_requires_actual_history_and_reference_conditions():
    case = {'required_tools': ['run_history'],
            'reference_codes': ['recent_pipeline_failures']}
    baseline = {'dataset_version': 'v1',
                'findings': [{'code': 'recent_pipeline_failures'}]}
    observed = result()
    assert not score_case(case, observed, baseline)['passed']
    observed['evidence'][0]['tool'] = 'run_history'
    assert not score_case(case, observed, baseline)['passed']
    observed['findings'][0]['code'] = 'recent_pipeline_failures'
    assert score_case(case, observed, baseline)['passed']


def test_protocol_error_is_contained_but_not_a_correct_model_refusal():
    case = {'expected_abstention': True}
    baseline = {'dataset_version': 'v1', 'findings': []}
    blocked = result() | {'status': 'abstained', 'findings': [],
                          'stop_reason': 'empty_or_limited_answer',
                          'limitations': ['empty_or_limited_answer']}
    scored = score_case(case, blocked, baseline)
    assert scored['safely_contained'] is True
    assert scored['passed'] is False
    refused = blocked | {'stop_reason': 'model_abstained',
                         'limitations': ['insufficient_evidence']}
    assert score_case(case, refused, baseline)['passed'] is True


def test_pair_score_rejects_identical_failures_and_incomplete_pairs():
    from llm_ops_lab.olist.evaluation import summarize_pairs

    cases = [{'case_id': 'a', 'pair_id': 'p', 'passed': False},
             {'case_id': 'b', 'pair_id': 'p', 'passed': False}]
    assert not summarize_pairs(cases)[0]['passed']
    cases[0]['passed'] = True
    assert not summarize_pairs(cases)[0]['passed']
    cases[1]['passed'] = True
    assert summarize_pairs(cases)[0]['passed']
    assert not summarize_pairs(cases[:1])[0]['passed']


def test_precision_and_no_tool_abstention_are_separate_hard_checks():
    baseline = {'dataset_version': 'v1', 'findings': []}
    case = {'required_codes': ['missing_origin_leads'],
            'allowed_codes': ['missing_origin_leads']}
    observed = result()
    assert score_case(case, observed, baseline)['passed']
    observed['findings'].append({'code': 'published_quality_warnings', 'evidence_ids': ['e1']})
    assert not score_case(case, observed, baseline)['passed']
    blocked = observed | {'status': 'abstained', 'stop_reason': 'model_abstained',
                          'findings': [], 'limitations': ['insufficient_evidence'],
                          'usage': {'tool_calls': 1}}
    case = {'expected_abstention': True, 'max_tool_calls': 0}
    assert not score_case(case, blocked, baseline)['passed']
    blocked['evidence'] = [{'evidence_id': 'e1', 'tool': 'version_evidence'}]
    assert not score_case(case, blocked, baseline)['passed']
    for invalid in (None, True, -1, '0'):
        blocked['usage']['tool_calls'] = invalid
        assert not score_case(case, blocked, baseline)['passed']
    blocked['usage']['tool_calls'] = 0
    assert score_case(case, blocked, baseline)['passed']


def test_execution_fingerprint_covers_queries_policy_runtime_and_lock_without_secrets():
    from llm_ops_lab.olist.evaluation import execution_hashes

    hashes = execution_hashes()
    assert {'src/llm_ops_lab/olist/tool_session.py', 'src/llm_ops_lab/olist/diagnostics.py',
            'src/llm_ops_lab/marketing/offline_model.py', 'src/llm_ops_lab/inference.py',
            'uv.lock'} <= hashes.keys()
    assert all(len(h) == 64 for h in hashes.values())
    assert not any('secrets' in name for name in hashes)


def test_pre_read_refusal_can_be_unversioned_only_without_any_data_access():
    baseline = {'dataset_version': 'v1', 'findings': []}
    case = {'expected_abstention': True, 'max_tool_calls': 0}
    refused = {'model_executed': True, 'diagnostic_mode': 'model_planned_investigation',
               'dataset_version': None, 'evidence': [], 'findings': [],
               'status': 'abstained', 'stop_reason': 'model_abstained',
               'limitations': ['insufficient_evidence'], 'usage': {'tool_calls': 0},
               'request_plan': {'disposition': 'unsafe', 'intents': []}}
    assert score_case(case, refused, baseline)['passed']
    for bad in ({'dataset_version': 'wrong'}, {'usage': {'tool_calls': 1}},
                {'evidence': [{'tool': 'version_evidence', 'evidence_id': 'e1'}]},
                {'stop_reason': 'invalid_request_plan'},
                {'request_plan': {'disposition': 'supported', 'intents': ['origin']}}):
        assert not score_case(case, refused | bad, baseline)['passed']


@pytest.mark.parametrize("args", [
    ["--provider", "nvidia"],
    ["--provider", "nvidia", "--remote-model", "nvidia/model", "--allow-remote"],
    ["--provider", "local", "--allow-remote"],
])
def test_remote_evaluation_requires_explicit_bounded_opt_in(monkeypatch, args):
    from llm_ops_lab.olist import evaluation
    monkeypatch.setattr("sys.argv", ["evaluation", *args])
    monkeypatch.setattr(evaluation, "connection_info", lambda *a, **k: pytest.fail("DB accessed"))
    with pytest.raises(SystemExit) as error:
        evaluation.main()
    assert error.value.code == 2


def test_remote_credentials_fail_before_database_or_network(tmp_path, monkeypatch):
    import json

    from llm_ops_lab.olist import evaluation
    from llm_ops_lab.olist.nvidia_planner import NvidiaPlannerError

    dataset = tmp_path / 'pilot.json'
    dataset.write_text(json.dumps({
        'version': 'pilot.v1', 'purpose': 'test',
        'cases': [{'id': 'unsafe', 'question': 'write', 'expected_abstention': True}],
    }))
    monkeypatch.delenv('NVIDIA_API_KEY', raising=False)
    monkeypatch.setattr('sys.argv', [
        'evaluation', '--root', str(tmp_path), '--dataset', str(dataset),
        '--engine', 'intent-v1', '--provider', 'nvidia', '--allow-remote',
        '--remote-model', 'nvidia/explicit-model',
    ])
    monkeypatch.setattr(evaluation, 'connection_info', lambda *a, **k: pytest.fail('DB accessed'))
    with pytest.raises(NvidiaPlannerError, match='credentials_missing'):
        evaluation.main()
    assert not (tmp_path / 'runs').exists()
