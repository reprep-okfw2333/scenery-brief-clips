"""Planner prompt eval (paths under tmp/planner_eval; rebuild briefs with make_briefs.py).

Results 2026-09-29, live z-ai/glm-5.3-flash, 9 briefs x 3: prompt v1 8/27 valid,
v2 25/27 (results_v1_v2.json).
"""
"""First-attempt validity of planner prompts against the live planner wire."""
import json, os, sys, time
from pathlib import Path
from collections import Counter
env = Path.home() / '.hermes' / '.env'
if not os.environ.get('OPENROUTER_API_KEY') and env.is_file():
    for line in env.read_text().splitlines():
        if line.startswith('OPENROUTER_API_KEY='):
            os.environ['OPENROUTER_API_KEY'] = line.split('=', 1)[1].strip().strip('"\'')
from scenery_brief_clips.brief import PLANNER_INSTRUCTION, render_planner, validate_query_plan
from scenery_brief_clips.planner import load_planner_wire, call_wired_planner
from scenery_brief_clips.vision_wire import parse_model_json
variants = {'v1': PLANNER_INSTRUCTION, 'v2': PLANNER_INSTRUCTION  # v2 is now the product prompt; v1 text is in git history (brief.py before 2026-09-29)}
only = sys.argv[1:] or list(variants)
trials = int(os.environ.get('TRIALS', '3'))
wire = load_planner_wire('.')
out = []
for name in only:
    for bp in sorted(Path('tmp/planner_eval/briefs').glob('*.json')):
        brief = json.loads(bp.read_text()); view = render_planner(brief)['search_view_json']
        for trial in range(trials):
            t0 = time.monotonic(); rec = {'variant': name, 'brief': bp.stem, 'trial': trial}
            try:
                text = call_wired_planner(wire, variants[name], view)
                rec['elapsed_s'] = round(time.monotonic() - t0, 1)
                try:
                    plan = parse_model_json(text)
                except Exception as exc:
                    rec.update(ok=False, code='not_json', error=str(exc)[:200]); out.append(rec); continue
                try:
                    validate_query_plan(plan, brief)
                    rec.update(ok=True, code='ok', queries=[q['query'] for q in plan['queries']])
                except Exception as exc:
                    rec.update(ok=False, code=getattr(exc, 'code', 'invalid'), error=str(exc)[:200],
                               queries=[q.get('query') for q in plan.get('queries', []) if isinstance(q, dict)] if isinstance(plan, dict) else None)
            except Exception as exc:
                rec.update(ok=False, code='call_error', error=str(exc)[:200])
            out.append(rec)
            print(json.dumps(rec, ensure_ascii=False), flush=True)
Path('tmp/planner_eval').joinpath(f"results_{'_'.join(only)}.json").write_text(json.dumps(out, indent=1, ensure_ascii=False))
for name in only:
    rs = [r for r in out if r['variant'] == name]
    print(name, 'valid', sum(r['ok'] for r in rs), '/', len(rs), dict(Counter(r['code'] for r in rs)))
