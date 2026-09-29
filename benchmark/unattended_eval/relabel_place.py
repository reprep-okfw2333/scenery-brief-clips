"""Relabel stored review strips with the CURRENT product strip prompt.

Offline check for the "place and setting are soft" prompt change (owner,
2026-09-29): every stored non-keep strip plus the first keep of each run
(controls) is labeled again through vision_wire.label_image with the run's own
theme (vision_wire._theme_from_run), live, on the wired model (vision.yaml).
No YouTube traffic. Needs OPENROUTER_API_KEY in the environment (load it from
~/.hermes/.env into this process only; never print it).

Usage (repo root):
    .venv/bin/python benchmark/unattended_eval/relabel_place.py OUT.json [--dry-run] [--baseline]
"""
import json
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, "src")
from scenery_brief_clips.vision_wire import (  # noqa: E402
    VisionWireError,
    _theme_from_run,
    brief_prompt,
    label_image,
    load_vision_wire,
)

RUNS = [
    "tmp/bench/batch1-R01-20260929T023532Z",
    "tmp/bench/batch1-R03-20260929T025628Z",
    "tmp/bench/batch1-R04-20260929T112307Z",
    "tmp/bench/batch1-R05-20260929T031949Z",
    "tmp/bench/batch1-R07-20260929T034551Z",
    "tmp/bench/batch1-R09-20260929T035859Z",
    "tmp/bench/batch1-R10-20260929T041603Z",
    "tmp/bench/jevab-R09-C-20260929T150632Z",
    "tmp/bench/jevab-R09-J-20260929T144720Z",
    "tmp/bench/step5-iceland-20260929T012602Z",
    "tmp/bench/heldout-horses-20260928T233348Z",
    "tmp/bench/step4-reddeer-20260929T003207Z",
    "data/runs/20260927T220608Z",
]


def run_dir_of(path: str) -> Path:
    p = Path(path)
    if (p / "shortlist_scores.json").is_file():
        return p
    found = sorted(p.glob("data/runs/*/shortlist_scores.json"))
    if len(found) != 1:
        raise SystemExit(f"cannot find one run dir under {p}")
    return found[0].parent


def items():
    out = []
    for name in RUNS:
        run = run_dir_of(name)
        review = json.loads((run / "review.json").read_text())
        stored = json.loads((run / "shortlist_scores.json").read_text())
        strips = {(m["video_id"], m["excerpt_index"]): m["strip"] for m in review["moments"]}
        first_keep = True
        for video_id, entries in stored.items():
            if not isinstance(entries, list):
                continue
            for entry in entries:
                keep = entry["match"] == "keep" and entry["geo"] != "conflicting"
                if keep and not first_keep:
                    continue
                if keep:
                    first_keep = False
                strip = Path(strips[(video_id, entry["excerpt_index"])])
                if not strip.is_absolute():
                    strip = run / strip
                out.append({"run": name, "run_dir": str(run), "video_id": video_id,
                            "excerpt_index": entry["excerpt_index"], "strip": str(strip),
                            "old": {k: entry.get(k) for k in ("match", "geo", "note")},
                            "control": keep})
    return out


def main() -> None:
    out_path = Path(sys.argv[1])
    todo = items()
    print(f"{len(todo)} strips ({sum(i['control'] for i in todo)} keep controls)")
    if "--dry-run" in sys.argv:
        run = Path(todo[0]["run_dir"])
        print(brief_prompt("strip", _theme_from_run(run)))
        return
    if "--baseline" in sys.argv:
        # Noise baseline: the prompt as it was before the place rule.
        import scenery_brief_clips.vision_wire as vw
        vw.PLACE_SOFT_RULE = ""
        todo = [i for i in todo if not i["control"]]
    wire = load_vision_wire(Path("."))

    def one(item):
        theme = _theme_from_run(Path(item["run_dir"]))
        try:
            new = label_image(wire, item["strip"], "strip", theme=theme)
            return {**item, "new": {k: new.get(k) for k in ("match", "geo", "note")}}
        except VisionWireError as exc:
            return {**item, "error": str(exc)}

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(one, todo))
    out_path.write_text(json.dumps(results, indent=1))
    ok = lambda lab: lab["match"] == "keep" and lab["geo"] != "conflicting"
    flips_up = [r for r in results if "new" in r and not ok(r["old"]) and ok(r["new"])]
    flips_down = [r for r in results if "new" in r and ok(r["old"]) and not ok(r["new"])]
    errors = [r for r in results if "error" in r]
    print(f"non-keep -> keep: {len(flips_up)}; keep -> non-keep: {len(flips_down)}; errors: {len(errors)}")


if __name__ == "__main__":
    main()
