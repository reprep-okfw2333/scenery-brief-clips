"""Replay the product gate (legacy and continuity_detector=blend) on the eval set."""
import json, time, sys
from pathlib import Path
from scenery_brief_clips.analyze import Excerpt
from scenery_brief_clips.continuity import ContinuitySettings, gate_excerpt
import os; d=Path(os.environ.get('EVAL_DIR','tmp/continuity_eval'))
raw=json.loads((d/'labels_sonnet.json').read_text()) if (d/'labels_sonnet.json').exists() else {}; corr=json.loads((d/'labels_corrections.json').read_text()) if (d/'labels_corrections.json').exists() else {}
lab={k:(corr.get(k) or v)['label'] for k,v in raw.items()}; import collections; lab=collections.defaultdict(lambda:'?',lab)
detector=sys.argv[1] if len(sys.argv)>1 else 'blend'
settings=ContinuitySettings(detector=detector)
out={}; t0=time.perf_counter()
for r in json.loads((d/'manifest.json').read_text()):
    ex=Excerpt(start_s=r['start_s'], end_s=r['end_s'], source_scene=(r['start_s'], r['end_s']))
    dec=gate_excerpt(ex, video_path=r['path'], analysis_span=(r['k_s'], r['k_s']+1e6), target_s=6.0, min_s=4.0, max_s=12.0,
                     settings=settings, mapping_k_s=r['k_s'])
    out[r['id']]={'label':lab[r['id']],'action':dec.action,'start_s':dec.start_s,'end_s':dec.end_s,'reason':dec.reason,
                  'events':list(dec.events),'orig':[r['start_s'],r['end_s']],'legacy':r['gate_action']}
el=time.perf_counter()-t0
(d/f'replay_{detector}.json').write_text(json.dumps(out,indent=1))
from collections import Counter
c=Counter((v['label'],v['action']) for v in out.values())
print(detector,'elapsed',round(el,1),'s'); [print(' ',k,n) for k,n in sorted(c.items())]
fa=[k for k,v in out.items() if v['label']=='transition' and v['action']=='keep']
fr=[k for k,v in out.items() if v['label']=='continuous' and v['action']!='keep']
print('missed transitions',len(fa),fa); print('flagged continuous',len(fr),fr)
