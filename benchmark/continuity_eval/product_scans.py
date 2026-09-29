"""Cache product-path scans (continuity_blend.read_scan) for the eval set."""
import json, pickle, time
from pathlib import Path
from scenery_brief_clips import continuity_blend as cb
d=Path('tmp/continuity_eval'); out={}; t0=time.perf_counter()
for r in json.loads((d/'manifest.json').read_text()):
    sc=cb.read_scan(r['path'], r['start_s']-r['k_s'], r['end_s']-r['k_s'])
    out[r['id']]=sc
pickle.dump(out, open(d/'product_scans.pkl','wb'))
print('scans', len(out), round(time.perf_counter()-t0,1),'s')
