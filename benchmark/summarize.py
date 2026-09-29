"""Summarize one benchmark run into <out>/summary.json and print it.

Usage: python benchmark/summarize.py <bench_root> <out_dir>
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path


def _load(path: Path):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


def main(root: Path, out: Path) -> dict:
    result = _load(out / "result.json") or {}
    run_dir = Path(result.get("run_dir") or "")
    if not run_dir.is_dir():
        runs = sorted((root / "data" / "runs").glob("*"))
        run_dir = runs[-1] if runs else root / "missing"

    stages = {s["stage"]: round(s["elapsed_s"], 2) for s in (result.get("timing") or {}).get("stages", [])}
    model_calls = sum(s.get("model_calls") or 0 for s in (result.get("timing") or {}).get("stages", []))

    candidates = _load(run_dir / "candidates.json") or []
    ranked = _load(run_dir / "ranked.json") or []
    scores = _load(run_dir / "vision_scores.json") or {}
    tile_labels: dict[str, int] = {}
    for tiles in scores.values():
        for t in tiles:
            tile_labels[t.get("label", "?")] = tile_labels.get(t.get("label", "?"), 0) + 1

    excerpts = _load(run_dir / "excerpts.json") or []
    n_ranges = sum(len(e.get("ranges", [])) for e in excerpts)
    n_excerpts = sum(len(e.get("excerpts", [])) for e in excerpts)
    analyzed_s = sum(r["span"][1] - r["span"][0] for e in excerpts for r in e.get("ranges", []))
    sources_without = sum(1 for e in excerpts if not e.get("excerpts"))
    continuity_rejected = sum(len(r.get("continuity_rejected") or []) for e in excerpts for r in e.get("ranges", []))

    shortlist = _load(run_dir / "shortlist.json") or {}
    clips = sorted(p.name for p in (root / "out").glob("*/clips/*.mp4"))
    verify_export = _load(run_dir / "verify_export.json") or {}

    discovery = _load(run_dir / "discovery.json") or {}
    jev_rank = discovery.get("jev_rank") or {}
    jev_notes = _load(run_dir / "jev_notes.json") or {}
    log_txt = (run_dir / "log.txt").read_text() if (run_dir / "log.txt").exists() else ""
    jev = {
        "rank": bool(jev_rank),
        "rank_counts": jev_rank.get("counts"),
        "rank_cost_usd": jev_rank.get("cost_usd"),
        "note_check": bool(jev_notes),
        "note_counts": jev_notes.get("counts"),
        "note_cost_usd": jev_notes.get("cost_usd"),
        # metadata lookups discovery made (cache hits included), in fetch order
        "metadata_lookups": len(re.findall(r"^(?:keep|reject|metadata error) ", log_txt, flags=re.M)),
    }

    time_txt = (out / "time.txt").read_text() if (out / "time.txt").exists() else ""
    wall = re.search(r"Elapsed \(wall clock\) time.*: (.+)", time_txt)
    rss = re.search(r"Maximum resident set size \(kbytes\): (\d+)", time_txt)

    summary = {
        "label": out.name,
        "exit_code": (out / "exit_code.txt").read_text().strip() if (out / "exit_code.txt").exists() else None,
        "status": result.get("status"),
        "stage": result.get("stage"),
        "error": result.get("error"),
        "missing": result.get("missing"),
        "wall_clock": wall.group(1) if wall else None,
        "max_rss_mb_main_process": round(int(rss.group(1)) / 1024, 1) if rss else None,
        "stage_sum_s": round(sum(stages.values()), 2),
        "stages_s": stages,
        "model_calls": model_calls,
        "jev": jev,
        "yield": {
            "candidates": len(candidates),
            "candidate_ids": [c.get("video_id") or c.get("id") for c in candidates],
            "ranked": len(ranked),
            "tile_labels": tile_labels,
            "analyzed_sources": len(excerpts),
            "analysis_ranges": n_ranges,
            "analysis_seconds_planned": round(analyzed_s, 2),
            "excerpts": n_excerpts,
            "sources_without_excerpts": sources_without,
            "continuity_rejected": continuity_rejected,
            "shortlist_counts": shortlist.get("counts"),
            "clips_delivered": len(clips),
            "clips": clips,
            "verify_export_ok": verify_export.get("ok"),
            "request_fulfilled": (shortlist.get("counts") or {}).get("request_fulfilled"),
        },
        "run_dir": str(run_dir),
    }
    (out / "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(json.dumps(summary, indent=2))
    return summary


if __name__ == "__main__":
    main(Path(sys.argv[1]), Path(sys.argv[2]))
