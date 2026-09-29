"""Tabulate a paired Jev A/B (benchmark/jev_ab.sh) and prepare blind clip judging.

Usage:
  .venv/bin/python benchmark/jev_ab_summary.py benchmark/runs/jev_ab-index-<utc>.tsv [--blind DIR]

Prints per brief: J (Jev rank + note check) vs C (no Jev, same frozen plan):
clips delivered / requested, ranked sources, sources that yielded no excerpt,
excerpts, shortlist exclusions, stage times, model calls, Jev calls/cost.
With --blind DIR, copies every delivered clip to DIR/clips/<neutral id>.mp4
and writes DIR/mapping.json (id -> brief, arm, clip) and DIR/requests.json
(id -> request text) so a judge sees no arm.
"""
from __future__ import annotations

import json
import random
import shutil
import sys
from pathlib import Path

PROJ = Path(__file__).resolve().parents[1]


def load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def arm_record(out_dir: Path) -> dict:
    summary = load(out_dir / "summary.json") or {}
    run_dir = Path(summary.get("run_dir") or "")
    root = PROJ / "tmp" / "bench" / out_dir.name
    ranked = load(run_dir / "ranked.json") or []
    excerpts = load(run_dir / "excerpts.json") or []
    shortlist = load(run_dir / "shortlist.json") or {}
    notes = load(run_dir / "jev_notes.json") or {}
    discovery = load(run_dir / "discovery.json") or {}
    y = summary.get("yield") or {}
    excluded = {}
    for e in shortlist.get("excluded") or []:
        for r in e.get("reasons") or []:
            key = "duplicate" if r.startswith("duplicate of") else r
            excluded[key] = excluded.get(key, 0) + 1
    return {
        "status": summary.get("status"),
        "wall": summary.get("wall_clock"),
        "stage_sum_s": summary.get("stage_sum_s"),
        "stages_s": summary.get("stages_s") or {},
        "model_calls": summary.get("model_calls"),
        "n_clips": (shortlist.get("n_clips_requested")),
        "delivered": y.get("clips_delivered"),
        "clips": sorted(str(p) for p in (root / "out").glob("*/clips/*.mp4")),
        "ranked": [(r.get("video_id"), r.get("title")) for r in ranked],
        "analyzed": len(excerpts),
        "no_excerpt": y.get("sources_without_excerpts"),
        "excerpts": y.get("excerpts"),
        "excluded": excluded,
        "jev": summary.get("jev") or {},
        "notes": notes.get("entries") or [],
        "request": (load(root / "brief.json") or {}).get("request_text"),
        "rejected_by_jev": [r.get("title") for r in discovery.get("rejected") or [] if r.get("reason") == "jev_reject"],
    }


def main() -> None:
    index = Path(sys.argv[1])
    blind = Path(sys.argv[sys.argv.index("--blind") + 1]) if "--blind" in sys.argv else None
    rows = [line.split("\t") for line in index.read_text().splitlines() if line.strip()]
    arms: dict = {}
    for rid, arm, rc, status, out in rows:
        if out != "missing":
            arms.setdefault(rid, {})[arm] = arm_record(Path(out))
    report = {}
    for rid, pair in arms.items():
        print(f"\n## {rid}: {next(iter(pair.values()))['request']}")
        for arm in ("J", "C"):
            a = pair.get(arm)
            if not a:
                print(f"  {arm}: missing")
                continue
            st = a["stages_s"]
            print(f"  {arm}: status {a['status']}, clips {a['delivered']}/{a['n_clips']}, wall {a['wall']}, stage sum {a['stage_sum_s']} s, "
                  f"model calls {a['model_calls']}; analyzed {a['analyzed']} (no excerpt {a['no_excerpt']}), excerpts {a['excerpts']}")
            print(f"     discover {st.get('discover')} s, label_tiles {st.get('label_tiles')}, analyze {st.get('analyze')}, "
                  f"label_strips {st.get('label_strips')}, export {st.get('export')}; exclusions {a['excluded']}")
            if a["jev"].get("rank") or a["jev"].get("note_check"):
                print(f"     jev rank {a['jev'].get('rank_counts')} ${a['jev'].get('rank_cost_usd')}; notes {a['jev'].get('note_counts')} "
                      f"${a['jev'].get('note_cost_usd')}; jev_reject {a['rejected_by_jev']}")
            for vid, title in a["ranked"]:
                print(f"       ranked {vid} {str(title)[:80]}")
        report[rid] = pair
    if blind:
        (blind / "clips").mkdir(parents=True, exist_ok=True)
        items = [(rid, arm, clip) for rid, pair in arms.items() for arm, a in pair.items() for clip in a["clips"]]
        random.Random(20260929).shuffle(items)
        mapping, requests = {}, {}
        for n, (rid, arm, clip) in enumerate(items, 1):
            cid = f"c{n:03d}"
            shutil.copy2(clip, blind / "clips" / f"{cid}.mp4")
            mapping[cid] = {"brief": rid, "arm": arm, "clip": clip}
            requests[cid] = arms[rid][arm]["request"]
        (blind / "mapping.json").write_text(json.dumps(mapping, indent=1))
        (blind / "requests.json").write_text(json.dumps(requests, indent=1))
        print(f"\nblind set: {len(items)} clips in {blind}")


if __name__ == "__main__":
    main()
