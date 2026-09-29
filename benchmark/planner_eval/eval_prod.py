"""Production path: planner.plan_queries (prompt v2 + one retry) against the live wire."""
import json, os, time
from pathlib import Path
from collections import Counter
env = Path.home() / '.hermes' / '.env'
if not os.environ.get('OPENROUTER_API_KEY') and env.is_file():
    for line in env.read_text().splitlines():
        if line.startswith('OPENROUTER_API_KEY='):
            os.environ['OPENROUTER_API_KEY'] = line.split('=', 1)[1].strip().strip('"\'')
from scenery_brief_clips.planner import load_planner_wire, plan_queries, PlannerError
wire = load_planner_wire('.')
trials = int(os.environ.get('TRIALS', '3')); out = []
for bp in sorted(Path('tmp/planner_eval/briefs').glob('*.json')):
    brief = json.loads(bp.read_text())
    for trial in range(trials):
        t0 = time.monotonic(); rec = {'brief': bp.stem, 'trial': trial}
        try:
            plan, prov = plan_queries(brief, wire)
            rec.update(ok=True, calls=prov['model_calls'], rejected=[r['code'] for r in prov['rejected_attempts']],
                       queries=[q['query'] for q in plan['queries']])
        except PlannerError as exc:
            rec.update(ok=False, calls=exc.model_calls, error=str(exc)[:200])
        rec['elapsed_s'] = round(time.monotonic() - t0, 1)
        out.append(rec); print(json.dumps(rec, ensure_ascii=False), flush=True)
Path('tmp/planner_eval/results_prod.json').write_text(json.dumps(out, indent=1, ensure_ascii=False))
print('plans', sum(r['ok'] for r in out), '/', len(out), 'calls', sum(r['calls'] for r in out),
      'retried', sum(1 for r in out if r['calls'] > 1), 'fails', Counter(r['brief'] for r in out if not r['ok']))
