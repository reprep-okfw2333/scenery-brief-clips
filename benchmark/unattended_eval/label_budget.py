import json, glob, os, sys, collections
sys.path.insert(0,"src")
from scenery_brief_clips.shortlist import moments_are_duplicates, parse_frame_hashes, build_shortlist
runs = sorted(set(os.path.dirname(p) for p in glob.glob("tmp/bench/*/data/runs/*/shortlist.json") + glob.glob("data/runs/*/shortlist.json")))
seen=set(); T=collections.Counter()
print(f"{'run':40} n  labeled | A:dedup-only labels sel | B:+stop(n) labels sel | C:+stop(n+2) labels sel | orig sel")
for r in runs:
    sl=json.load(open(f"{r}/shortlist.json")); sc=json.load(open(f"{r}/shortlist_scores.json"))
    rv=json.load(open(f"{r}/review.json")); ex=json.load(open(f"{r}/excerpts.json"))
    key=json.dumps(sc,sort_keys=True)
    if key in seen: continue  # identical seeded replays
    seen.add(key)
    n=int(sl["n_clips_requested"])
    lab={(v,e["excerpt_index"]):e for v,es in sc.items() if isinstance(es,list) for e in es}
    sig={(m["video_id"],m["excerpt_index"]):parse_frame_hashes(m) for m in rv["moments"]}
    ok=lambda e: e["match"]=="keep" and e["geo"]!="conflicting" and not e.get("note_violation")
    # source order = excerpts.json row order (rank order); interleave sources round robin
    per=[[ (row["video_id"],i) for i in range(len(row.get("excerpts") or []))] for row in ex if isinstance(row,dict)]
    rr=[]; 
    while any(per):
        for p in per:
            if p: rr.append(p.pop(0))
    def sim(stop):
        kept=[]; used=0
        for i in range(0,len(rr),4):   # waves of 4 concurrent calls
            if stop and len(kept)>=stop: break
            wave=[k for k in rr[i:i+4]]
            for k in wave:
                if any(moments_are_duplicates(sig[k],sig[q]) for q in kept): continue  # skipped, no call
                used+=1
                if ok(lab[k]): kept.append(k)
        return used, min(n,len(kept))
    # dedup-only must be judged in the product's order (sorted keys) to match exactly
    kept=[];used=0
    for k in sorted(sig):
        if any(moments_are_duplicates(sig[k],sig[q]) for q in kept): continue
        used+=1
        if ok(lab[k]): kept.append(k)
    a=(used,min(n,len(kept)))
    b=sim(n); c=sim(n+2); o=len(sl["selected"])
    name=r.replace("tmp/bench/","").split("/data/runs/")[0][:40]
    print(f"{name:40} {n:<2} {len(lab):<7} | {a[0]:>5} {a[1]:>3}          | {b[0]:>5} {b[1]:>3}          | {c[0]:>5} {c[1]:>3}           | {o}")
    T.update(dict(labeled=len(lab),A=a[0],Asel=a[1],B=b[0],Bsel=b[1],C=c[0],Csel=c[1],orig=o))
print(dict(T))
