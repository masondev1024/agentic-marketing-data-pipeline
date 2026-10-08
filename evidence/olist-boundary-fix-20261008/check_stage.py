import hashlib
import json
import sys
from pathlib import Path

ROOT=Path.cwd(); WORK=ROOT/'work/olist-boundary'
stage=sys.argv[1]
log=(WORK/f'{stage}.log').read_text().splitlines()
batch=Path(log[-1])
if not batch.is_dir(): raise SystemExit('No completed batch receipt')
summary=json.loads((batch/'summary.json').read_text())
contracts=json.loads((WORK/'all-semantic-contracts.json').read_text())['cases']
rows=[]
for case in summary['cases']:
    value=json.loads((batch/f"{case['case_id']}.json").read_text())
    c=contracts[case['case_id']];plan=value.get('request_plan') or {}
    checks={'structural':case['passed'],
            'disposition':plan.get('disposition') in c.get('allowed_dispositions',[c['expected_disposition']]),
            'exact_intents':sorted(plan.get('intents',[]))==sorted(c['exact_supported_intents'])}
    for finding in value.get('findings',[]):
        if finding['code'] in {'missing_origin_leads','seller_sample_coverage_unknown'}:
            intent='origin' if finding['code']=='missing_origin_leads' else 'coverage'
            entry=next(e for e in value['evidence'] if e['tool']=='channel_funnel')
            data=entry['data']; observed=finding.get('observed_counts',{})
            numerator=(sum(r['lead_count'] for r in data if r['origin'] is None) if intent=='origin'
                       else sum(r['unlinked_contract_count'] for r in data))
            denominator=sum(r['lead_count' if intent=='origin' else 'contract_count'] for r in data)
            checks['numerator_denominator']= (observed.get('numerator'),observed.get('denominator'))==(numerator,denominator)
            checks['pinned_snapshot_counts']=(numerator,denominator)==((60,8000) if intent=='origin' else (462,842))
            checks['displayed_denominator']=f'{numerator}/{denominator}' in finding['summary']
        if finding['code']=='seller_sample_coverage_unknown':
            checks['unknown_not_zero']='unknown, not zero' in finding['summary']
        if finding['code']=='recent_pipeline_failures':
            checks['no_unproven_causality']='does not establish root cause' in finding['summary']
    rows.append({'case_id':case['case_id'],'passed':all(checks.values()),'checks':checks,'plan':plan})
result={'stage':stage,'batch':str(batch.relative_to(ROOT)),'passed':sum(c['passed']for c in rows),
        'total':len(rows),'cases':rows,'source_sha256':summary['source_sha256'],
        'semantic_contract_sha256':hashlib.sha256((WORK/'all-semantic-contracts.json').read_bytes()).hexdigest()}
(WORK/f'{stage}-review.json').write_text(json.dumps(result,ensure_ascii=False,indent=2))
if result['passed']!=result['total']:
    (WORK/f'{stage}-exit.json').write_text(json.dumps({'exit_code':1,'reason':'supplemental_semantic_gate_failed'}))
print(json.dumps({'stage':stage,'passed':result['passed'],'total':result['total'],
                  'failures':[c for c in rows if not c['passed']]},ensure_ascii=False))
raise SystemExit(int(result['passed']!=result['total']))
