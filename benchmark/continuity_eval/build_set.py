"""Build a labeled-evaluation set of real continuity-gate decisions.

For each candidate moment the gate decided on (rejected, trimmed or kept) in
the given runs, write a 2 fps contact sheet of the ORIGINAL candidate window
and a manifest row with the gate's verdict. Labels are added separately
(labels.json: id -> "continuous" | "transition" | "unclear").

Usage: python benchmark/continuity_eval/build_set.py OUT_DIR RUN_DIR [RUN_DIR...]
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

from scenery_brief_clips.analysis_cache import analysis_marker_path


def _k(path: Path, span) -> float:
    try:
        marker = json.loads(analysis_marker_path(path).read_text())
        if isinstance(marker.get("first_pts_ms"), int):
            return marker["first_pts_ms"] / 1000.0
    except (OSError, ValueError):
        pass
    return float(span[0])


def main(out: Path, runs: list[Path]) -> None:
    out.mkdir(parents=True, exist_ok=True)
    rows = []
    for run in runs:
        tag = run.parent.parent.parent.name if run.name == "seeded" else run.name
        for row in json.loads((run / "excerpts.json").read_text()):
            vid = row["video_id"]
            by_key = {rg["cache_key"]: rg for rg in row["ranges"]}
            items = []
            for rg in row["ranges"]:
                for rej in rg.get("continuity_rejected") or []:
                    items.append((rg, rej["start_s"], rej["end_s"], "reject", rej))
            for ex in row["excerpts"]:
                meta = ex.get("continuity") or {}
                rg = by_key.get(ex["analysis_cache_key"])
                if rg is None:
                    continue
                items.append((rg, meta.get("original_start_s", ex["start_s"]),
                              meta.get("original_end_s", ex["end_s"]), meta.get("action", "keep"), meta))
            for rg, start, end, action, meta in items:
                path = Path(rg["path"])
                if not path.is_file():
                    continue
                k = _k(path, rg["span"])
                item_id = f"{tag}__{vid}__{start:.3f}"
                sheet = out / f"{item_id}.jpg"
                if not sheet.is_file():
                    subprocess.run(
                        # -t BEFORE -i limits the input: tile emits one frame at the
                        # end, so an output -t would not stop it reading past the window.
                        ["ffmpeg", "-v", "error", "-y", "-ss", f"{start - k:.3f}", "-t", f"{end - start:.3f}",
                         "-i", str(path), "-vf", "fps=2,scale=240:-1,tile=6x4",
                         "-frames:v", "1", str(sheet)],
                        check=True,
                    )
                rows.append({
                    "id": item_id, "run": str(run), "video_id": vid, "path": str(path),
                    "k_s": k, "start_s": start, "end_s": end, "gate_action": action,
                    "gate_reason": meta.get("reason"), "gate_meta": meta, "sheet": str(sheet),
                })
    (out / "manifest.json").write_text(json.dumps(rows, indent=1))
    by = {}
    for r in rows:
        by[r["gate_action"]] = by.get(r["gate_action"], 0) + 1
    print(len(rows), by)


if __name__ == "__main__":
    main(Path(sys.argv[1]), [Path(p) for p in sys.argv[2:]])
