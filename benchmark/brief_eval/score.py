"""Score agent-written briefs with the old (HEAD) and new validators."""
import json, sys, importlib.util
from pathlib import Path
spec = importlib.util.spec_from_file_location('brief_old', 'tmp/brief_eval/brief_old.py'); old = importlib.util.module_from_spec(spec); spec.loader.exec_module(old)
from scenery_brief_clips import brief as new
d = Path(sys.argv[1]); notes = json.loads((d / 'notes.json').read_text())
for rid in sorted(notes):
    n = notes[rid]; p = d / f'{rid}.json'
    if not p.exists():
        print(f"{rid} no brief (asked: {n.get('asked_user')!r:.60})"); continue
    b = json.loads(p.read_text()); res = []
    for name, mod in (('old', old), ('new', new)):
        try: mod.validate_brief(b); res.append(f'{name}=ok')
        except Exception as e: res.append(f'{name}=FAIL[{str(e)[:70]}]')
    print(rid, ' | '.join(res), '| cnx:', len(n.get('could_not_express') or []))
