"""Score the Jev answers against observed outcomes and blind reference labels (offline).

Usage: .venv/bin/python benchmark/jev_eval/analyze.py [--details]
Reads data/{sources,strips}.json, data/jev_*.json and, when present, data/ref_*.json.
"""
from __future__ import annotations

import json
import sys
from collections import Counter, defaultdict
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data"
TOP_K = 5  # rank slots per run in the batch config (max_rank_videos)


def load(name):
    path = DATA / name
    return json.loads(path.read_text()) if path.is_file() else None


def auc(pos, neg):
    if not pos or not neg:
        return None
    wins = sum(1.0 if p > n else 0.5 if p == n else 0.0 for p in pos for n in neg)
    return round(wins / (len(pos) * len(neg)), 3)


def p_usable(rec):
    return rec["answers"]["usable"]["noul"]


def outcome_label(src):
    """productive / dead / None (not analyzed, or its run failed before shortlist)."""
    o = src["outcome"]
    if not o or not o["run_completed"]:
        return None
    verdicts = [d["verdict"] for d in o["delivered"] if d["verdict"]]
    if verdicts and all(v == "bad" for v in verdicts):
        return "dead"
    return "productive" if o["strip_keep_geo_ok"] > 0 else "dead"


def main() -> None:
    details = "--details" in sys.argv
    sources = load("sources.json")
    ref = {r["id"]: r for r in (load("ref_sources.json") or [])}
    print(f"# Sources ({len(sources)} candidates, 10 briefs)\n")
    for variant in ("flat", "full"):
        jev = {r["id"]: r for r in load(f"jev_sources-{variant}.json")["results"]}
        rows = []
        for s in sources:
            sid = f"{s['brief']}:{s['video_id']}"
            rows.append((s, sid, jev[sid], outcome_label(s), ref.get(sid)))
        pos = [p_usable(j) for _, _, j, o, _ in rows if o == "productive"]
        neg = [p_usable(j) for _, _, j, o, _ in rows if o == "dead"]
        print(f"## state = {variant}")
        print(f"- outcome AUC P(usable), productive {len(pos)} vs dead {len(neg)}: {auc(pos, neg)}")
        for q in ("subject", "conditions"):
            print(f"  - AUC P({q}): {auc([j['answers'][q]['noul'] for _, _, j, o, _ in rows if o == 'productive'], [j['answers'][q]['noul'] for _, _, j, o, _ in rows if o == 'dead'])}")
        if ref:
            ry = [p_usable(j) for _, _, j, _, r in rows if r and r["usable"] == "yes"]
            rn = [p_usable(j) for _, _, j, _, r in rows if r and r["usable"] == "no"]
            print(f"- reference AUC P(usable), ref yes {len(ry)} vs ref no {len(rn)}: {auc(ry, rn)}")
            kind_agree = sum(1 for _, _, j, _, r in rows if r and j["answers"]["kind"]["choice"] == r["kind"])
            place_agree = sum(1 for _, _, j, _, r in rows if r and j["answers"]["place"]["choice"] == r["place"])
            print(f"- agreement with reference: kind {kind_agree}/{len(ref)}, place {place_agree}/{len(ref)}")
        # Replay the rank slot choice: search order vs Jev order, per brief.
        by_brief = defaultdict(list)
        for row in rows:
            by_brief[row[0]["brief"]].append(row)
        tot = Counter()
        lines = []
        for brief, group in sorted(by_brief.items()):
            base = sorted(group, key=lambda r: r[0]["search_order"])[:TOP_K]
            jevk = sorted(group, key=lambda r: -p_usable(r[2]))[:TOP_K]
            for name, pick in (("search", base), ("jev", jevk)):
                c = Counter(r[3] or "unobserved" for r in pick)
                rc = Counter((r[4] or {}).get("usable", "?") for r in pick)
                tot.update({f"{name}_{k}": v for k, v in c.items()})
                tot.update({f"{name}_ref_{k}": v for k, v in rc.items()})
                lines.append(f"  {brief:8} {name:6} productive {c['productive']} dead {c['dead']} unobserved {c['unobserved']}"
                             + (f" | ref yes {rc['yes']} no {rc['no']} unsure {rc['unsure']}" if ref else ""))
        print(f"- top-{TOP_K} replay totals (10 briefs x {TOP_K}): " + ", ".join(f"{k} {v}" for k, v in sorted(tot.items())))
        if details:
            print("\n".join(lines))
        print()
    if details:
        jev = {r["id"]: r for r in load("jev_sources-full.json")["results"]}
        print("## analyzed sources (full state)")
        for s in sources:
            o = outcome_label(s)
            if o:
                a = jev[f"{s['brief']}:{s['video_id']}"]["answers"]
                print(f"  {o:10} P(usable) {a['usable']['noul']:.2f} subj {a['subject']['noul']:.2f} "
                      f"place {a['place']['choice']:9} kind {a['kind']['choice']:24} {s['brief']:8} {s['meta']['title'][:60]}")
        print()

    strips = load("strips.json")
    jev = load("jev_strips.json")["results"]
    sref = {r["id"]: r for r in (load("ref_strips.json") or [])}
    print(f"# Strips ({len(strips)} vision notes)\n")
    rows = [(s, j, sref.get(j["id"])) for s, j in zip(strips, jev)]
    for thr in (0.5, 0.7):
        flag = lambda j: j["answers"]["violation"]["noul"] >= thr  # noqa: E731
        judged = [(s, j) for s, j, _ in rows if s["eye_verdict"]]
        bad = [x for x in judged if x[0]["eye_verdict"] == "bad"]
        ok = [x for x in judged if x[0]["eye_verdict"] != "bad"]
        keeps = [(s, j) for s, j, _ in rows if s["match"] == "keep"]
        rejects = [(s, j) for s, j, _ in rows if s["match"] == "reject"]
        print(f"## threshold P(violation) >= {thr}")
        print(f"- delivered clips judged by eye: flags {sum(flag(j) for _, j in bad)}/{len(bad)} bad, "
              f"{sum(flag(j) for _, j in ok)}/{len(ok)} good or acceptable")
        print(f"- vision match=keep strips flagged: {sum(flag(j) for _, j in keeps)}/{len(keeps)}; "
              f"match=reject strips flagged: {sum(flag(j) for _, j in rejects)}/{len(rejects)}")
        if sref:
            tp = sum(1 for _, j, r in rows if r and flag(j) and r["violation"] == "yes")
            fp = sum(1 for _, j, r in rows if r and flag(j) and r["violation"] == "no")
            fn = sum(1 for _, j, r in rows if r and not flag(j) and r["violation"] == "yes")
            print(f"- vs reference: tp {tp} fp {fp} fn {fn}")
    if sref:
        ry = [j["answers"]["violation"]["noul"] for _, j, r in rows if r and r["violation"] == "yes"]
        rn = [j["answers"]["violation"]["noul"] for _, j, r in rows if r and r["violation"] == "no"]
        print(f"- reference AUC P(violation): {auc(ry, rn)} (yes {len(ry)}, no {len(rn)})")
    if details:
        print("\n## keep strips, by P(violation)")
        for s, j, r in sorted(rows, key=lambda x: -x[1]["answers"]["violation"]["noul"]):
            if s["match"] == "keep":
                print(f"  {j['answers']['violation']['noul']:.2f} eye={s['eye_verdict'] or '-':10} ref={(r or {}).get('violation', '?'):3} "
                      f"{s['brief']:7} {s['note'][:120]}")


if __name__ == "__main__":
    main()
