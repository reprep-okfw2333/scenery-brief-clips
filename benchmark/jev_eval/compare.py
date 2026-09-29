"""Compare Jev source question sets / state variants / ranking scores (offline).

Usage: .venv/bin/python benchmark/jev_eval/compare.py
"""
from __future__ import annotations

from collections import Counter, defaultdict

from analyze import TOP_K, auc, load, outcome_label

FILES = [
    ("v1 flat", "jev_sources-flat.json"),
    ("v1 snippet", "jev_sources-snippet.json"),
    ("v1 full", "jev_sources-full.json"),
    ("v2 snippet", "jev_sources-snippet-v2.json"),
    ("v2 full", "jev_sources-full-v2.json"),
]


def p_choice(a, q, label):
    return (a[q].get("probabilities") or {}).get(label, 0.0)


SCORES = {
    "usable": lambda a: a["usable"]["noul"],
    "usable*subject": lambda a: a["usable"]["noul"] * a["subject"]["noul"],
    "usable*(1-place_diff)": lambda a: a["usable"]["noul"] * (1 - p_choice(a, "place", "different")),
    "mean(usable,subject,cond)": lambda a: (a["usable"]["noul"] + a["subject"]["noul"] + a["conditions"]["noul"]) / 3,
}


def main() -> None:
    sources = load("sources.json")
    ref = {r["id"]: r for r in load("ref_sources.json")}
    print(f"{'variant':12} {'score':26} {'AUC out':>7} {'AUC ref':>7} | top-{TOP_K}: ref yes/unsure/no | observed prod/dead | ref-no picks")
    for name, fname in FILES:
        doc = load(fname)
        if doc is None:
            continue
        jev = {r["id"]: r["answers"] for r in doc["results"]}
        for sname, fn in SCORES.items():
            score = {sid: fn(a) for sid, a in jev.items()}
            rows = [(s, f"{s['brief']}:{s['video_id']}") for s in sources]
            pos = [score[i] for s, i in rows if outcome_label(s) == "productive"]
            neg = [score[i] for s, i in rows if outcome_label(s) == "dead"]
            ry = [score[i] for _, i in rows if ref[i]["usable"] == "yes"]
            rn = [score[i] for _, i in rows if ref[i]["usable"] == "no"]
            by_brief = defaultdict(list)
            for s, i in rows:
                by_brief[s["brief"]].append((s, i))
            c = Counter()
            for group in by_brief.values():
                for s, i in sorted(group, key=lambda x: -score[x[1]])[:TOP_K]:
                    c["ref_" + ref[i]["usable"]] += 1
                    c[outcome_label(s) or "unobserved"] += 1
            print(f"{name:12} {sname:26} {auc(pos, neg):7} {auc(ry, rn):7} | {c['ref_yes']:3}/{c['ref_unsure']:2}/{c['ref_no']:2}"
                  f"            | {c['productive']:3}/{c['dead']:2}")
    base = Counter()
    by_brief = defaultdict(list)
    for s in sources:
        by_brief[s["brief"]].append(s)
    for group in by_brief.values():
        for s in sorted(group, key=lambda x: x["search_order"])[:TOP_K]:
            base["ref_" + ref[f"{s['brief']}:{s['video_id']}"]["usable"]] += 1
            base[outcome_label(s) or "unobserved"] += 1
    print(f"{'search order':39} {'':>7} {'':>7} | {base['ref_yes']:3}/{base['ref_unsure']:2}/{base['ref_no']:2}"
          f"            | {base['productive']:3}/{base['dead']:2}")
    # How often does v2 call a productive source dark/still, and what kinds win?
    for name, fname in FILES[3:]:
        jev = {r["id"]: r["answers"] for r in load(fname)["results"]}
        kinds = Counter()
        for s in sources:
            o = outcome_label(s)
            if o:
                kinds[(jev[f"{s['brief']}:{s['video_id']}"]["kind"]["choice"], o)] += 1
        print(name, "kind x outcome:", dict(sorted(kinds.items())))


if __name__ == "__main__":
    main()
