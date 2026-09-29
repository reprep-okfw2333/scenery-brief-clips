"""Build the offline Jev evaluation set from existing benchmark runs (no network).

Writes benchmark/jev_eval/data/sources.json (one record per brief x candidate video,
with trimmed metadata and, for analyzed sources, the observed outcome) and
data/strips.json (one record per vision strip label, with the eye verdict where
the clip was delivered and judged).
"""
from __future__ import annotations

import glob
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(__file__).resolve().parent / "data"

# brief dir -> glob of bench roots that ran it
RUNS = {
    "R01": "batch1-R01-*", "R03": "batch1-R03-*", "R04": "batch1-R04-*", "R05": "batch1-R05-*",
    "R07": "batch1-R07-*", "R09": "batch1-R09-*", "R10": "batch1-R10-*", "R11": "batch1-R11-*",
    "reddeer": "step4-reddeer-*", "iceland": "step5-iceland-*",
}
BRIEFS = {k: f"benchmark/batch1/{k}/brief.json" for k in RUNS if k.startswith("R")}
BRIEFS.update(reddeer="benchmark/step4-reddeer/brief.json", iceland="benchmark/step5-iceland/brief.json")


def load(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def trim_meta(info: dict) -> dict:
    formats = [f for f in info.get("formats") or []
               if isinstance(f, dict) and (f.get("vcodec") or "none") != "none" and isinstance(f.get("height"), (int, float))]
    best = max(formats, key=lambda f: (f.get("height") or 0), default=None)
    return {
        "title": info.get("title"),
        "channel": info.get("channel") or info.get("uploader"),
        "duration_s": info.get("duration"),
        "views": info.get("view_count"),
        "upload_date": info.get("upload_date"),
        "best_resolution": f"{best.get('width')}x{best.get('height')}" if best else None,
        "tags": list(info.get("tags") or [])[:25],
        "categories": info.get("categories"),
        "chapters": [c.get("title") for c in info.get("chapters") or []][:40],
        "description": str(info.get("description") or "").strip()[:1200],
    }


def main() -> None:
    labels = load(ROOT / "benchmark/batch1/judge/labels.json")
    verdicts: dict = {}
    for lab in labels:
        verdicts[(lab["run"], lab["clip"])] = lab
    sources: dict = {}
    strips: list = []
    for key, pattern in RUNS.items():
        brief = load(ROOT / BRIEFS[key])
        for run in sorted(glob.glob(str(ROOT / "tmp/bench" / pattern / "data/runs/*"))):
            run = Path(run)
            cache = run.parents[1] / "cache" / "metadata"
            cands = load(run / "candidates.json")
            ranked = {r["video_id"]: r for r in load(run / "ranked.json")} if (run / "ranked.json").is_file() else {}
            scores = load(run / "shortlist_scores.json") if (run / "shortlist_scores.json").is_file() else {}
            shortlist = load(run / "shortlist.json") if (run / "shortlist.json").is_file() else {"selected": []}
            vision = load(run / "vision_scores.json") if (run / "vision_scores.json").is_file() else {}
            for order, cand in enumerate(cands):
                vid = cand["video_id"]
                meta_path = cache / f"{vid}.json"
                if not meta_path.is_file():
                    continue
                rec = sources.setdefault((key, vid), {
                    "brief": key, "video_id": vid, "search_order": order,
                    "meta": trim_meta(load(meta_path)), "runs": [], "outcome": None,
                })
                rec["runs"].append(run.parents[2].name)
                if vid not in ranked:
                    continue
                tiles = Counter(t.get("label") for t in vision.get(vid, []))
                dark = sum(1 for t in vision.get(vid, []) if "dark" in str(t.get("note")))
                strip_rows = scores.get(vid, []) if isinstance(scores.get(vid), list) else []
                delivered = []
                for sel in shortlist.get("selected", []):
                    if sel["video_id"] != vid:
                        continue
                    clip = next((c for (r, c) in verdicts if r == key and c.startswith(f"{vid}_e{sel['excerpt_index']}_")), None)
                    delivered.append({"excerpt_index": sel["excerpt_index"],
                                      "verdict": verdicts[(key, clip)]["verdict"] if clip else None})
                outcome = {
                    "run": run.parents[2].name,
                    # False when the run failed before shortlist (R11 windows bug, R04 empty span).
                    "run_completed": (run / "shortlist.json").is_file(),
                    "rank_reason": ranked[vid].get("reason"),
                    "tiles": dict(tiles), "dark_tiles": dark,
                    "strips": len(strip_rows),
                    "strip_keep": sum(1 for s in strip_rows if s.get("match") == "keep"),
                    "strip_keep_geo_ok": sum(1 for s in strip_rows if s.get("match") == "keep" and s.get("geo") != "conflicting"),
                    "delivered": delivered,
                }
                # Prefer the run that got furthest (has strips / shortlist).
                if rec["outcome"] is None or (outcome["run_completed"], outcome["strips"]) > (
                        rec["outcome"]["run_completed"], rec["outcome"]["strips"]):
                    rec["outcome"] = outcome
                for s in strip_rows:
                    sel = next((d for d in delivered if d["excerpt_index"] == s["excerpt_index"]), None)
                    strips.append({
                        "brief": key, "run": run.parents[2].name, "video_id": vid,
                        "excerpt_index": s["excerpt_index"], "match": s.get("match"), "geo": s.get("geo"),
                        "scene_type": s.get("scene_type"), "note": s.get("note"),
                        "delivered": sel is not None, "eye_verdict": sel["verdict"] if sel else None,
                    })
    OUT.mkdir(parents=True, exist_ok=True)
    briefs = {k: {f: load(ROOT / p).get(f) for f in ("request_text", "theme_text", "scene", "geography", "n_clips")}
              for k, p in BRIEFS.items()}
    (OUT / "briefs.json").write_text(json.dumps(briefs, indent=1, ensure_ascii=False))
    (OUT / "sources.json").write_text(json.dumps(list(sources.values()), indent=1, ensure_ascii=False))
    (OUT / "strips.json").write_text(json.dumps(strips, indent=1, ensure_ascii=False))
    analyzed = [s for s in sources.values() if s["outcome"]]
    print(f"sources {len(sources)} (analyzed {len(analyzed)}), strips {len(strips)} "
          f"(delivered+judged {sum(1 for s in strips if s['eye_verdict'])})")


if __name__ == "__main__":
    main()
