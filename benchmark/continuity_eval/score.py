"""Score the current continuity gate and candidate rules against blind labels.

Window-level: does the rule flag a transition anywhere in the candidate window?
  false reject = labeled continuous but flagged; false accept = labeled
  transition but not flagged. Unclear labels are excluded.
The current gate's verdict is "flagged" for reject AND trim (trim means it found
instability somewhere in the window).

Usage: python benchmark/continuity_eval/score.py EVAL_DIR [SIGNALS_FILE]
"""

from __future__ import annotations

import itertools
import json
import statistics
import sys
from pathlib import Path


def candidate_flags(sig: dict, *, res_abs: float, spike: float, color_jump: float) -> bool:
    res = [p["res"] for p in sig["pairs"]]
    med = max(statistics.median(res), 1.0)
    for p in sig["pairs"]:
        if p["res"] >= res_abs and p["res"] / med >= spike:
            return True
        if p["color"] >= color_jump:
            return True
    return False


def score(labels: dict, flags: dict) -> dict:
    fr = [k for k, l in labels.items() if l == "continuous" and flags[k]]
    fa = [k for k, l in labels.items() if l == "transition" and not flags[k]]
    n_c = sum(1 for l in labels.values() if l == "continuous")
    n_t = sum(1 for l in labels.values() if l == "transition")
    return {"false_reject": len(fr), "of_continuous": n_c, "false_accept": len(fa), "of_transition": n_t,
            "fr_ids": fr, "fa_ids": fa}


def main(eval_dir: Path, signals_name: str) -> None:
    manifest = {r["id"]: r for r in json.loads((eval_dir / "manifest.json").read_text())}
    raw = json.loads((eval_dir / "labels_sonnet.json").read_text())
    labels = {k: v["label"] for k, v in raw.items() if v["label"] in ("continuous", "transition")}
    signals = json.loads((eval_dir / signals_name).read_text())

    gate = {k: manifest[k]["gate_action"] in ("reject", "trim") for k in labels}
    gate_reject = {k: manifest[k]["gate_action"] == "reject" for k in labels}
    print("current gate (reject|trim = flagged):", {k: v for k, v in score(labels, gate).items() if not k.endswith("ids")})
    print("current gate (reject only = flagged):", {k: v for k, v in score(labels, gate_reject).items() if not k.endswith("ids")})

    results = []
    for res_abs, spike, color_jump in itertools.product((8, 10, 12, 15, 20), (2.5, 3, 3.5, 4, 5), (18, 20, 24, 28, 35)):
        flags = {k: candidate_flags(signals[k], res_abs=res_abs, spike=spike, color_jump=color_jump) for k in labels}
        s = score(labels, flags)
        results.append(((s["false_accept"], s["false_reject"]), (res_abs, spike, color_jump), s))
    results.sort(key=lambda item: (item[0][0] * 2 + item[0][1], item[0]))
    print("best candidates (false_accept weighted x2):")
    for (fa, fr), params, s in results[:8]:
        print(f"  res_abs={params[0]} spike={params[1]} color_jump={params[2]}  FA={fa}/{s['of_transition']}  FR={fr}/{s['of_continuous']}")
    best = results[0][2]
    print("best FA ids:", best["fa_ids"])
    print("best FR ids:", best["fr_ids"])


if __name__ == "__main__":
    main(Path(sys.argv[1]), sys.argv[2] if len(sys.argv) > 2 else "signals_6fps.json")
