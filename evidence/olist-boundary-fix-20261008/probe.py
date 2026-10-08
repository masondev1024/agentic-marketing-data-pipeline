import hashlib
import json
import sys
from pathlib import Path

from llm_ops_lab.olist.intent_plan import parse_plan, plan_messages, prompt_bytes
from llm_ops_lab.olist.nvidia_planner import NvidiaPlanner

root = Path.cwd()
destination = root / 'work/olist-boundary' / f'{sys.argv[1]}-probe.json'
if destination.exists():
    raise SystemExit('Existing probe must not be overwritten')
manifest = json.loads((root / 'data/olist/investigation-super-acceptance-v1.json').read_text())
question = next(c['question'] for c in manifest['cases'] if c['id'] == 'super_rootcause_deploy')
key_path = root / 'work/olist/secrets/nvidia-api-key.txt'
if key_path.stat().st_mode & 0o077:
    raise SystemExit('Invalid key permissions')
receipt = {'prompt_sha256': hashlib.sha256(prompt_bytes()).hexdigest(), 'expected': 'unavailable_root_cause', 'calls': []}
with NvidiaPlanner(model_id='nvidia/nemotron-3-super-120b-a12b', api_key=key_path.read_text().strip(), max_total_calls=10) as model:
    for index in range(10):
        entry = {'index': index, 'status': 'attempted'}
        receipt['calls'].append(entry)
        destination.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        result = model.generate(plan_messages(question), timeout=30)
        plan = parse_plan(result['output'])
        entry.update(status='returned', labels=json.loads(result['output'])['requests'], disposition=plan.disposition,
                     passed=plan.disposition == receipt['expected'], prompt_tokens=result['prompt_tokens'], output_tokens=result['output_tokens'])
        destination.write_text(json.dumps(receipt, ensure_ascii=False, indent=2))
        print(json.dumps(entry), flush=True)
        if sum(c.get('passed') is False for c in receipt['calls']) >= 3:
            raise SystemExit('Repeated failure: stopping probe for diagnosis')
