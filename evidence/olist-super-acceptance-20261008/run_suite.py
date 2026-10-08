"""One frozen evaluation stage per fresh process; no implicit retries or resume."""
import hashlib
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from llm_ops_lab.olist import evaluation
from llm_ops_lab.olist.nvidia_planner import NvidiaPlanner, NvidiaPlannerError

ROOT=Path.cwd()
WORK=ROOT/'work/olist-super-acceptance'
STAGES=('acceptance','repeat-1','repeat-2','repeat-3')
if len(sys.argv)!=2 or sys.argv[1] not in STAGES:
    raise SystemExit('Specify one valid stage')
stage=sys.argv[1]
gate=json.loads((WORK/'gate.json').read_text())
for name,digest in gate['source_sha256'].items():
    if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:
        raise SystemExit('Candidate source changed; refusing stale evaluation')
freeze=json.loads((WORK/'manifest-freeze.json').read_text())
# The author records artifact hashes; coordinator separately pins runner manifests below.
pinned=json.loads((WORK/'runner-manifests.json').read_text())
for name,digest in pinned.items():
    if hashlib.sha256((ROOT/name).read_bytes()).hexdigest()!=digest:
        raise SystemExit('Evaluation manifest changed')
if stage!='acceptance':
    previous=STAGES[STAGES.index(stage)-1]
    status=json.loads((WORK/f'{previous}-exit.json').read_text())
    if status['exit_code']!=0:
        permission=WORK/'diagnostic-continuation.json'
        prior_review=WORK/f'{previous}-review.json'
        if not permission.exists() or not prior_review.exists():
            raise SystemExit('Prior stage failed; no additional model calls')
        reviewed=json.loads(prior_review.read_text())
        if not all(c['checks']['structural'] and c['checks']['exact_intents'] for c in reviewed['cases']):
            raise SystemExit('Structural/action failure: diagnostic continuation refused')
    # Do not spend repeated calls after the same semantic failure has recurred three times.
    failures={}
    for report in WORK.glob('*-review.json'):
        for c in json.loads(report.read_text())['cases']:
            if not c['passed']: failures[c['case_id']]=failures.get(c['case_id'],0)+1
    if any(n>=3 for n in failures.values()):
        raise SystemExit('Same failure observed three times; diagnostic loop stopped')
receipt=WORK/f'{stage}-receipts.json'
if receipt.exists():
    raise SystemExit('Stage already attempted; explicit investigation required before rerun')
key_path=ROOT/'work/olist/secrets/nvidia-api-key.txt'
if key_path.stat().st_mode & 0o077:
    raise SystemExit('Key file permissions invalid')
key=key_path.read_text().strip()
if not key:
    raise SystemExit('Key missing')
os.environ['NVIDIA_API_KEY']=key
prior_calls=sum(len(json.loads(p.read_text())['calls']) for p in WORK.glob('*-receipts.json'))
calls=[]
def persist():
    value={'stage':stage,'model':gate['model'],'calls':calls,'calls_before_stage':prior_calls}
    temp=receipt.with_suffix('.tmp')
    temp.write_text(json.dumps(value,ensure_ascii=False,indent=2));temp.replace(receipt)
original=NvidiaPlanner.generate

def generate(self,messages,timeout):
    if prior_calls+len(calls)>=gate['max_total_provider_calls']:
        raise SystemExit('Global provider call budget exhausted')
    call={'status':'attempted','at':datetime.now(timezone.utc).isoformat()}
    calls.append(call);persist()
    try:
        result=original(self,messages,timeout)
        call.update(status='returned',prompt_tokens=result['prompt_tokens'],
                    output_tokens=result['output_tokens'],seconds=result['generation_seconds'],
                    output_sha256=hashlib.sha256(result['output'].encode()).hexdigest())
        try:
            labels=json.loads(result['output']).get('requests',[])
            from llm_ops_lab.olist.intent_plan import REQUEST_LABELS
            if type(labels) is list and all(type(x) is str and x in REQUEST_LABELS for x in labels):
                call['request_labels']=labels
        except (ValueError,AttributeError):
            pass
        return result
    except NvidiaPlannerError as error:
        call.update(status='adapter_error',code=error.code)
        # Availability failures do not establish semantic quality; stop immediately.
        if error.code in {'authentication_failed','rate_limited','upstream_unavailable',
                          'unexpected_http_status','upstream_transport_error','upstream_timeout'}:
            raise SystemExit('Provider stage stopped: '+error.code) from None
        raise
    finally:
        persist()
NvidiaPlanner.generate=generate
manifest=('data/olist/investigation-super-acceptance-v1.json' if stage=='acceptance'
          else 'work/olist-super-acceptance/critical-repeat.json')
sys.argv=['evaluation','--engine','intent-v1','--provider','nvidia','--allow-remote',
          '--remote-model',gate['model'],'--dataset',manifest]
try:
    code=evaluation.main()
except SystemExit as error:
    (WORK/f'{stage}-exit.json').write_text(json.dumps({'exit_code':1,'error':str(error)}))
    raise
(WORK/f'{stage}-exit.json').write_text(json.dumps({'exit_code':code}))
raise SystemExit(code)
