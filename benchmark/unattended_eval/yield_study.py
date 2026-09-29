import json, glob, collections, os, sys
runs = sorted(set(os.path.dirname(p) for p in glob.glob("tmp/bench/*/data/runs/*/shortlist.json") + glob.glob("data/runs/*/shortlist.json")))
print(f"{'run':44} n  geo        src mom  keep unc rej gconf dup beyond sel | mom/src keep/src")
tot=collections.Counter()
for r in runs:
    try:
        sl=json.load(open(f"{r}/shortlist.json")); sc=json.load(open(f"{r}/shortlist_scores.json"))
        ex=json.load(open(f"{r}/excerpts.json")); con=json.load(open(f"{r}/constraint.json"))
    except Exception as e:
        print(r, "ERR", e); continue
    n=int(sl["n_clips_requested"]); rows=[x for x in ex if isinstance(x,dict)]
    src=sum(1 for x in rows if x.get("status")=="complete")
    mom=sum(len(x.get("excerpts") or []) for x in rows)
    m=collections.Counter(); g=collections.Counter(); gconf_keep=0
    for vid,ents in sc.items():
        if not isinstance(ents,list): continue
        for e in ents:
            m[e["match"]]+=1; g[e["geo"]]+=1
            if e["match"]=="keep" and e["geo"]=="conflicting": gconf_keep+=1
    reasons=collections.Counter()
    for e in sl["excluded"]:
        for rr in e["reasons"]: reasons["dup" if rr.startswith("duplicate") else rr]+=1
    sel=len(sl["selected"])
    beyond=sum(v for k,v in reasons.items() if "beyond" in k or "not needed" in k or "enough" in k)
    geo=con.get("geo_requirement")
    name=r.replace("tmp/bench/","").split("/data/runs/")[0][:44]
    print(f"{name:44} {n:<2} {str(geo)[:10]:10} {src:<3} {mom:<4} {m['keep']:<4} {m['uncertain']:<3} {m['reject']:<3} {gconf_keep:<5} {reasons['dup']:<3} {beyond:<6} {sel:<3} | {mom/max(src,1):5.1f} {m['keep']/max(src,1):5.1f}  geo={dict(g)}")
    for k in ("n","src","mom","sel"): pass
    tot.update(dict(n=n,src=src,mom=mom,keep=m['keep'],unc=m['uncertain'],rej=m['reject'],gconf_keep=gconf_keep,sel=sel,dup=reasons['dup'],beyond=beyond,short=max(0,n-sel)))
    tot.update({"geo_"+k:v for k,v in g.items()})
    tot["fulfilled"]+= sel>=n; tot["runs"]+=1
print(dict(tot))
print("all exclusion reason strings seen:", sorted({rr for r in runs for e in json.load(open(f'{r}/shortlist.json'))['excluded'] for rr in e['reasons'] if not rr.startswith('duplicate')}))
