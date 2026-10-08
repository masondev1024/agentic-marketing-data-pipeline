"""Real offline model smoke evaluation; preserves failures and baseline separately."""

import argparse
import hashlib
import time
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .config import connection_info
from .diagnostics import diagnose
from .eval_manifest import MAX_MANIFEST_BYTES, EvalManifestError, parse_eval_manifest
from .pipeline import save_report


def score_case(case: dict, result: dict, baseline: dict) -> dict:
    """Check structured contracts, not the truth of arbitrary model prose."""
    findings = result.get('findings', [])
    codes = {item['code'] for item in findings}
    known = {item['evidence_id'] for item in result.get('evidence', [])}
    tools = {item['tool'] for item in result.get('evidence', [])}
    baseline_codes = {item['code'] for item in baseline.get('findings', [])}
    required = set(case.get('required_codes', [])) | (
        set(case.get('reference_codes', [])) & baseline_codes
    )
    plan = result.get('request_plan') or {}
    pre_read_refusal = (
        case.get('expected_abstention') is True
        and result.get('diagnostic_mode') == 'model_planned_investigation'
        and plan.get('disposition') in {
            'unsafe', 'unavailable_root_cause', 'out_of_scope', 'ambiguous'}
        and plan.get('intents') == []
        and result.get('dataset_version') is None
        and not result.get('evidence')
        and type(result.get('usage', {}).get('tool_calls')) is int
        and result['usage']['tool_calls'] == 0
    )
    checks = {
        'model_executed': result.get('model_executed') is True,
        'references_valid': all(
            item.get('evidence_ids') and set(item['evidence_ids']) <= known for item in findings
        ),
        'required_codes': required <= codes,
        'required_tools': set(case.get('required_tools', [])) <= tools,
    }
    if pre_read_refusal:
        checks['no_data_access_on_refusal'] = True
    else:
        checks['same_published_version'] = (
            result.get('dataset_version') == baseline.get('dataset_version'))
    if case.get('allowed_codes') is not None:
        checks['no_unrelated_findings'] = codes <= set(case['allowed_codes'])
    if case.get('max_tool_calls') is not None:
        calls = result.get('usage', {}).get('tool_calls')
        checks['tool_scope'] = (type(calls) is int and 0 <= calls <= case['max_tool_calls'])
    if case.get('expected_refusal'):
        checks['expected_refusal'] = (
            result.get('status') == 'abstained'
            and case['expected_refusal'] in result.get('limitations', [])
            and result.get('stop_reason') == 'model_abstained'
            and 'definitions' in tools
            and not findings
        )
    elif case.get('expected_abstention'):
        checks['expected_abstention'] = (
            result.get('status') == 'abstained' and not findings
            and result.get('stop_reason') == 'model_abstained'
            and 'insufficient_evidence' in result.get('limitations', [])
        )
    else:
        checks['completed'] = result.get('status') == 'completed'
    return {'passed': all(checks.values()), 'checks': checks,
            'safely_contained': result.get('status') == 'abstained' and not findings,
            'required_codes': sorted(required)}


def summarize_pairs(results: list[dict]) -> list[dict]:
    """All members must meet their own expectations; equal failures are not success."""
    pairs: dict[str, list[dict]] = {}
    for result in results:
        if result.get('pair_id'):
            pairs.setdefault(result['pair_id'], []).append(result)
    return [
        {'pair_id': name, 'passed': len(members) == 2 and all(m['passed'] for m in members),
         'case_ids': [m['case_id'] for m in members]}
        for name, members in sorted(pairs.items())
    ]


def execution_hashes() -> dict[str, str]:
    """Identify the local decision path; never read credential files."""
    root = Path(__file__).parents[3]
    files = [
        'src/llm_ops_lab/olist/' + name for name in (
            'agent.py', 'agent_prompt.txt', 'evaluation.py', 'eval_manifest.py',
            'diagnostics.py', 'tool_session.py', 'lineage.py', 'config.py',
            'intent_agent.py', 'intent_plan.py', 'intent_prompt.txt', 'nvidia_planner.py',
        )
    ] + [
        'src/llm_ops_lab/marketing/offline_model.py',
        'src/llm_ops_lab/marketing/model_worker.py',
        'src/llm_ops_lab/inference.py', 'src/llm_ops_lab/cli.py',
        'data/olist/metric-policy-v1.json', 'uv.lock',
    ]
    return {name: hashlib.sha256((root / name).read_bytes()).hexdigest() for name in files}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, default=Path.cwd())
    parser.add_argument('--dataset', type=Path)
    parser.add_argument('--engine', choices=('action-v5', 'intent-v1'), default='action-v5')
    parser.add_argument('--provider', choices=('local', 'nvidia'), default='local')
    parser.add_argument('--remote-model', help='Explicit NVIDIA catalog model ID; no default')
    parser.add_argument(
        '--allow-remote', action='store_true',
        help='Allow sending planner instructions and evaluation questions to NVIDIA')
    parser.add_argument('--timeout', type=float, default=60.0)
    args = parser.parse_args()
    if args.provider == 'nvidia' and (
        not args.allow_remote or not args.remote_model or args.engine != 'intent-v1'
    ):
        parser.error('NVIDIA evaluation requires --allow-remote --remote-model '
                     'and --engine intent-v1')
    if args.provider == 'local' and (args.remote_model or args.allow_remote):
        parser.error('Remote options require --provider nvidia')
    root = args.root.resolve()
    if not 0 < args.timeout <= 300:
        parser.error('timeout must be in (0, 300]')
    dataset_path = args.dataset or root / 'data/olist/investigation-eval-v1.json'
    try:
        with dataset_path.open('rb') as handle:
            raw = handle.read(MAX_MANIFEST_BYTES + 1)
        dataset = parse_eval_manifest(raw)
    except (OSError, EvalManifestError) as error:
        parser.error(str(error))
    if args.provider == 'nvidia':
        from .nvidia_planner import NvidiaPlanner
        backend = NvidiaPlanner(model_id=args.remote_model, max_total_calls=2 * len(dataset.cases))
    else:
        from llm_ops_lab.marketing.offline_model import OfflineModel
        backend = OfflineModel()
    destination = root / 'runs/olist-investigations' / str(uuid.uuid4())
    destination.mkdir(parents=True)
    (destination / 'evaluation.json').write_bytes(raw)
    source_hashes = execution_hashes()
    dsn = connection_info(root, reader=True)
    baseline = diagnose(dsn)
    save_report(destination / 'baseline.json', baseline)
    if baseline['status'] != 'completed':
        print('Baseline unavailable; no model started.')
        return 1

    if args.engine == 'intent-v1':
        from .intent_agent import investigate
    else:
        from .agent import investigate

    results = []
    with backend as model:
        identity = model.identity

        def generate(messages, remaining):
            return model.generate(messages, remaining) | {
                'model_id': identity['model_id'], 'model_revision': identity['model_revision']}

        for item in dataset.cases:
            case = asdict(item) | {'id': item.case_id}
            started = time.monotonic()
            result = investigate(dsn, case['question'], generate,
                                 version_id=baseline['dataset_version'],
                                 timeout_seconds=args.timeout)
            result['model'] = identity
            save_report(destination / f"{case['id']}.json", result)
            score = score_case(case, result, baseline)
            results.append({
                'case_id': case['id'], 'persona': item.persona, 'pair_id': item.pair_id,
                **score, 'stop_reason': result.get('stop_reason'),
                'seconds': time.monotonic() - started, 'usage': result.get('usage', {}),
                'repair_requests': sum(t.get('repair_requested') is True
                                       for t in result.get('trace', [])),
                'prompt': result.get('prompt'),
            })
            print(f"{case['id']}: {'PASS' if score['passed'] else 'FAIL'}", flush=True)
    report = {
        'schema_version': 'olist.investigation-evaluation.v4', 'engine': args.engine,
        'provider': args.provider,
        'scorer_sha256': source_hashes['src/llm_ops_lab/olist/evaluation.py'],
        'source_sha256': source_hashes,
        'created_at': datetime.now(UTC).isoformat(), 'model': identity,
        'dataset_version': baseline['dataset_version'],
        'evaluation_version': dataset.version,
        'evaluation_sha256': hashlib.sha256(raw).hexdigest(),
        'purpose': dataset.purpose, 'provenance': dataset.provenance, 'cases': results,
        'pair_results': summarize_pairs(results),
        'passed': sum(item['passed'] for item in results), 'total': len(results),
        'baseline_mode': 'deterministic evidence reference; not question-aware agent',
        'quality_claim': 'structured smoke checks only; manual review required',
    }
    save_report(destination / 'summary.json', report)
    print(destination)
    return int(report['passed'] != report['total'])


if __name__ == '__main__':
    raise SystemExit(main())
