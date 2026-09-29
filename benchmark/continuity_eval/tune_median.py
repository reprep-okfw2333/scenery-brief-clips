import json, pickle, statistics as stt
from pathlib import Path
import numpy as np
from scenery_brief_clips import continuity_blend as cb
d=Path('tmp/continuity_eval')
raw=json.loads((d/'labels_sonnet.json').read_text()); corr=json.loads((d/'labels_corrections.json').read_text())
lab={k:(corr.get(k) or v)['label'] for k,v in raw.items()}
scans=pickle.load(open(d/'product_scans.pkl','rb'))
fx=cb.read_scan('tests/fixtures/continuity_late_cut.mp4',0.0,8.0)
scans['FIXTURE_late_cut']=fx; lab['FIXTURE_late_cut']='fixture'
pre={}
for k,sc in scans.items():
    g=[cb._gray(f) for f in sc.frames]; mask=cb.dynamic_mask(g)
    cuts=cb.cut_records(sc.frames,g)
    bl={}
    for lag in cb.BLEND_LAGS:
        recs=cb.blend_records(g,mask,lag,0,len(g)-1)
        for r in recs:
            t=r['t']; r['near']=float(min(np.abs(g[t]-g[t-lag]).mean(),np.abs(g[t]-g[t+lag]).mean()))
            r['ends']=cb.masked_residual(g[t-lag],g[t+lag],mask) if (r['score']>=1.2 and sc.first<=t<=sc.last) else 0
        bl[lag]=recs
    pre[k]=(sc,cuts,bl)
def ref(vals, i, mode, floor):
    if mode=='scan': v=vals
    elif mode=='nofreeze': v=[x for x in vals if x>=0.5] or vals
    else: v=[x for x in vals[max(0,i-12):i+13] if x>=0.5] or vals
    return max(stt.median(v),floor)
def decide(k,mode):
    sc,cuts,bl=pre[k]; ev=[]
    rv=[c['res'] for c in cuts]
    for c in cuts:
        i=c['i']
        if i<sc.first or i+1>sc.last: continue
        if (c['res']>=cb.CUT_RESIDUAL and c['res']/ref(rv,i,mode,1.0)>=cb.CUT_SPIKE) or c['color']>=cb.CUT_COLOR: ev.append(cb.Event('cut',i,0))
    for lag,recs in bl.items():
        sv=[r['score'] for r in recs]
        nv=[r['score'] for r in recs if r['near']>=0.5] if mode!='scan' else sv
        typ=max(stt.median(nv or sv),0.5)
        for r in recs:
            t=r['t']
            if not(sc.first<=t<=sc.last and cb.BLEND_ALPHA_MIN<=r['alpha']<=1-cb.BLEND_ALPHA_MIN): continue
            if r['score']>=cb.BLEND_SCORE and r['score']/typ>=cb.BLEND_SPIKE and r['ends']>=cb.BLEND_ENDS: ev.append(cb.Event('dissolve',t,lag))
    if not ev: return 'keep',ev
    runs=cb.clean_runs(sc.first,sc.last,ev); best=max((sc.times[j]-sc.times[i] for i,j in runs),default=0)
    return ('trim' if best>=4.0 else 'reject'),ev
for mode in ('scan','nofreeze','local'):
    res={k:decide(k,mode) for k in lab}
    fa=[k for k in lab if lab[k]=='transition' and res[k][0]=='keep']
    rj=[k for k in lab if lab[k]=='continuous' and res[k][0]=='reject']
    tr=[k for k in lab if lab[k]=='continuous' and res[k][0]=='trim']
    fxe=[(e.kind,round(pre['FIXTURE_late_cut'][0].times[e.index],2)) for e in res['FIXTURE_late_cut'][1]]
    print(f"{mode:9} transitions kept={len(fa)} continuous rejected={len(rj)} trimmed={len(tr)} fixture events={fxe}  kept={[x[-30:] for x in fa]}")
