"""Tune blend thresholds on cached product scans; decisions via the product clean_runs logic."""
import json, pickle, statistics as stt, itertools
from pathlib import Path
import numpy as np
from scenery_brief_clips import continuity_blend as cb
d=Path('tmp/continuity_eval')
raw=json.loads((d/'labels_sonnet.json').read_text()); corr=json.loads((d/'labels_corrections.json').read_text())
lab={k:(corr.get(k) or v)['label'] for k,v in raw.items()}
scans=pickle.load(open(d/'product_scans.pkl','rb'))
pre={}
for k,sc in scans.items():
    gray=[cb._gray(f) for f in sc.frames]; mask=cb.dynamic_mask(gray)
    cuts=cb.cut_records(sc.frames,gray); med=max(stt.median(c['res'] for c in cuts),1.0)
    cut_ev=[cb.Event('cut',c['i'],0) for c in cuts if sc.first<=c['i'] and c['i']+1<=sc.last and ((c['res']>=cb.CUT_RESIDUAL and c['res']/med>=cb.CUT_SPIKE) or c['color']>=cb.CUT_COLOR)]
    bl={}
    for lag in cb.BLEND_LAGS:
        recs=cb.blend_records(gray,mask,lag,0,len(gray)-1)
        typ=max(stt.median(r['score'] for r in recs),0.5) if recs else 1
        cand=[]
        for r in recs:
            t=r['t']
            if sc.first<=t<=sc.last and cb.BLEND_ALPHA_MIN<=r['alpha']<=1-cb.BLEND_ALPHA_MIN and r['score']>=1.2:
                ends=cb.masked_residual(gray[t-lag],gray[t+lag],mask)
                cand.append((r['score'],r['score']/typ,ends,t))
        bl[lag]=cand
    pre[k]=(sc,cut_ev,bl)
pickle.dump(pre,open(d/'tune_pre.pkl','wb'))
def decide(k,thr,spike,ends_min=cb.BLEND_ENDS,min_s=4.0):
    sc,cut_ev,bl=pre[k]
    ev=list(cut_ev)+[cb.Event('dissolve',t,lag) for lag,c in bl.items() for (s,sp,e,t) in c if s>=thr and sp>=spike and e>=ends_min]
    if not ev: return 'keep',ev
    runs=cb.clean_runs(sc.first,sc.last,ev)
    best=max((sc.times[j]-sc.times[i] for i,j in runs),default=0)
    return ('trim' if best>=min_s else 'reject'),ev
def evaluate(thr,spike):
    res={k:decide(k,thr,spike)[0] for k in lab}
    fa=[k for k in lab if lab[k]=='transition' and res[k]=='keep']
    frj=[k for k in lab if lab[k]=='continuous' and res[k]=='reject']
    ftr=[k for k in lab if lab[k]=='continuous' and res[k]=='trim']
    return fa,frj,ftr
for thr,spike in itertools.product((1.3,1.35,1.4,1.5),(1.0,1.15,1.2,1.25,1.3,1.4,1.5)):
    fa,frj,ftr=evaluate(thr,spike)
    print(f"thr={thr} spike={spike}: missed={len(fa)} cont_reject={len(frj)} cont_trim={len(ftr)}  missed={[x[-28:] for x in fa]}")
print('--- dissolve evidence (max score, spike) per item with any candidate')
for k in lab:
    sc,cut_ev,bl=pre[k]; c=[x for L in bl for x in bl[L]]
    if c: 
        b=max(c,key=lambda x:x[1]); print(f"{lab[k]:11} maxscore={max(x[0] for x in c):.2f} maxspike={b[1]:.2f} ends@={b[2]:.1f} cutev={len(cut_ev)} {k[-40:]}")
