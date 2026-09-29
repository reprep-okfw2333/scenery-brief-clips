"""Blend-fit (cross-dissolve) signals for the continuity evaluation set.

A cross-dissolve frame is a linear mix of the shots on either side of it, so
for a middle frame b and frames a, c taken LAG before and after it:

  alpha = argmin |b - (alpha*a + (1-alpha)*c)|   (least squares, clipped 0..1)
  blend = mean |b - (alpha*a + (1-alpha)*c)|      small inside a dissolve
  near  = min(mean|b-a|, mean|b-c|)               large inside a dissolve
  ends  = motion-compensated residual between a and c (different shots)

Camera motion (pans, marching crowds) makes b far from a and c too, but a mix
of a and c does not explain b, so blend stays high. `score = near / (blend+1)`.
Pixels that are static across the window (logos, tickers, letterbox) are
masked out. Decoded frames are cached in EVAL_DIR/frames_{fps}fps_{width}.npz.

With PAD > 0 the decode starts PAD s before the window and ends PAD s after it
(clamped to the file), so transitions at the window edges get neighbours on
both sides; each item records `win` = [first, last] frame index inside the
window. Output: dissolve_{fps}fps[_pad{PAD}].json with the cut signals
(`pairs`: res/color per adjacent pair) and blend tracks.

Usage: python benchmark/continuity_eval/dissolve.py EVAL_DIR [FPS] [WIDTH] [PAD]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).parent))
from signals import compensated_residual, dynamic_mask, frames, pair_signals  # noqa: E402


def load_frames(eval_dir: Path, rows: list[dict], fps: float, width: int, pad: float = 0.0):
    """id -> (frames, (first, last) window frame indices)."""
    tag = f"_pad{pad:g}" if pad else ""
    cache = eval_dir / f"frames_{int(fps)}fps_{width}{tag}.npz"
    if cache.is_file():
        data = np.load(cache)
        if all(r["id"] in data.files for r in rows):
            return {r["id"]: (data[r["id"]], tuple(int(x) for x in data[r["id"] + "__win"])) for r in rows}
    out, save = {}, {}
    for row in rows:
        lo, hi = row["start_s"] - row["k_s"], row["end_s"] - row["k_s"]
        dec_lo = max(0.0, lo - pad)
        fr = frames(row["path"], dec_lo, hi + pad, fps, width)
        first = int(round((lo - dec_lo) * fps))
        last = min(len(fr) - 1, first + int((hi - lo) * fps))
        out[row["id"]] = (np.stack(fr), (first, last))
        save[row["id"]] = out[row["id"]][0]
        save[row["id"] + "__win"] = np.array([first, last])
        print("decoded", row["id"], len(fr), (first, last), flush=True)
    np.savez_compressed(cache, **save)
    return out


def blend_track(stack: np.ndarray, lag: int) -> list[dict]:
    """Per middle frame t: alpha, blend residual, near distance, ends residual."""
    frames_bgr = list(stack)
    mask = dynamic_mask(frames_bgr)
    gray = [cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames_bgr]
    sel = mask if mask is not None else np.ones_like(gray[0], dtype=bool)
    out = []
    for t in range(lag, len(gray) - lag):
        a, b, c = gray[t - lag][sel], gray[t][sel], gray[t + lag][sel]
        d = a - c
        denom = float((d * d).sum())
        alpha = float(np.clip(((b - c) * d).sum() / denom, 0.0, 1.0)) if denom > 1e-6 else 0.5
        blend = float(np.abs(b - (alpha * a + (1 - alpha) * c)).mean())
        near = float(min(np.abs(b - a).mean(), np.abs(b - c).mean()))
        ends = compensated_residual(frames_bgr[t - lag], frames_bgr[t + lag], mask)
        out.append({"t": t, "alpha": round(alpha, 3), "blend": round(blend, 3), "near": round(near, 3),
                    "ends": round(ends, 3), "score": round(near / (blend + 1.0), 3)})
    return out


def main(eval_dir: Path, fps: float, width: int, pad: float) -> None:
    rows = json.loads((eval_dir / "manifest.json").read_text())
    stacks = load_frames(eval_dir, rows, fps, width, pad)
    out = {}
    for row in rows:
        st, win = stacks[row["id"]]
        item = {f"lag{lag}": blend_track(st, lag) for lag in (2, 3, 6) if len(st) > 2 * lag}
        item["win"] = list(win)
        if pad:
            item["pairs"] = [{k: p[k] for k in ("res", "color")} for p in
                             (pair_signals(st[i], st[i + 1]) for i in range(len(st) - 1))]
        out[row["id"]] = item
    tag = f"_pad{pad:g}" if pad else ""
    (eval_dir / f"dissolve_{int(fps)}fps{tag}.json").write_text(json.dumps(out))
    print("wrote", len(out))


if __name__ == "__main__":
    main(Path(sys.argv[1]), float(sys.argv[2]) if len(sys.argv) > 2 else 6.0,
         int(sys.argv[3]) if len(sys.argv) > 3 else 160,
         float(sys.argv[4]) if len(sys.argv) > 4 else 0.0)
