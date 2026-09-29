"""Replay the PRODUCT strip labeler (budget on) on copies of stored runs.

A fake caller answers each strip with the label stored in the run's
shortlist_scores.json, so no model is called. Prints labels used and the
rebuilt shortlist's selection count vs the original. Usage (repo root):
    .venv/bin/python benchmark/unattended_eval/replay_budget.py WORKDIR
"""
import glob, json, os, shutil, sys
from pathlib import Path

sys.path.insert(0, "src")
from scenery_brief_clips.shortlist import build_shortlist, validate_label_payload
from scenery_brief_clips.vision_wire import VisionWire, label_review_strips

work = Path(sys.argv[1])
runs = sorted(set(os.path.dirname(p) for p in glob.glob("tmp/bench/*/data/runs/*/shortlist.json")
                  + glob.glob("data/runs/*/shortlist.json")))
wire = VisionWire(backend="openai-api", model="replay")
tot = {"orig_labels": 0, "labels": 0, "orig_sel": 0, "sel": 0, "same_selected": 0, "runs": 0}
for run in runs:
    run = Path(run)
    dst = work / run.as_posix().replace("/", "_")
    if dst.exists():
        shutil.rmtree(dst)
    dst.mkdir(parents=True)
    for name in ("review.json", "constraint.json", "excerpts.json", "shortlist.json", "shortlist_scores.json"):
        shutil.copy(run / name, dst / name)
    review = json.loads((dst / "review.json").read_text())
    stored = json.loads((dst / "shortlist_scores.json").read_text())
    by_strip = {}
    for m in review["moments"]:
        ents = [e for e in stored.get(m["video_id"], []) if e["excerpt_index"] == m["excerpt_index"]]
        strip = Path(m["strip"])
        src = strip if strip.is_absolute() else run / strip
        local = dst / "strips" / f"{m['video_id']}_{m['excerpt_index']}.jpg"
        local.parent.mkdir(exist_ok=True)
        shutil.copy(src, local)
        m["strip"] = str(local)
        lab = dict(ents[0])
        lab.pop("excerpt_index")
        note = lab.get("note", "")
        if note.startswith("continuity not cleared: "):
            lab["note"] = note[len("continuity not cleared: "):]
        for k in ("note_check", "note_violation"):
            lab.pop(k, None)
        by_strip[str(local)] = json.dumps(lab)
    (dst / "review.json").write_text(json.dumps(review))
    calls = []
    def caller(_wire, image_path, _prompt):
        calls.append(image_path)
        return by_strip[str(image_path)]
    result = label_review_strips(dst, wire, caller=caller, budget=True)
    assert not result["failures"], result["failures"]
    labels = validate_label_payload(json.loads(json.dumps(result["scores"])))
    orig = json.loads((dst / "shortlist.json").read_text())
    n = int(orig["n_clips_requested"])
    doc = build_shortlist(json.loads((dst / "excerpts.json").read_text()), review, labels, n, orig["bindings"])
    o_sel = {(e["video_id"], e["excerpt_index"]) for e in orig["selected"]}
    n_sel = {(e["video_id"], e["excerpt_index"]) for e in doc["selected"]}
    n_orig_labels = sum(len(v) for k, v in stored.items() if isinstance(v, list))
    name = run.as_posix().replace("tmp/bench/", "").split("/data/runs/")[0][:40]
    print(f"{name:40} n={n:<2} labels {n_orig_labels:>3} -> {len(calls):>3}   selected {len(o_sel)} -> {len(n_sel)}"
          f"  same_set={o_sel == n_sel}  excl={doc['counts']['n_excluded_by_reason'] if len(n_sel) < n else ''}")
    tot["orig_labels"] += n_orig_labels; tot["labels"] += len(calls)
    tot["orig_sel"] += len(o_sel); tot["sel"] += len(n_sel); tot["same_selected"] += o_sel == n_sel; tot["runs"] += 1
print(tot)
