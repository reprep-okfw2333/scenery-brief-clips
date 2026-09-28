"""Per-item frame-difference signals for the continuity evaluation set.

For each manifest item: decode the original candidate window sequentially at
FPS, grayscale at WIDTH px, and record for each adjacent pair:
  dhash  - 64-bit dHash hamming distance (what the gate uses, but denser)
  color  - mean-RGB distance (what the gate uses)
  mad    - raw mean absolute luma difference (0..255)
  res    - mean absolute difference after undoing the global translation found
           by phase correlation (camera pan/tilt compensated), overlap only
  shift  - magnitude of that translation in px, resp - phase-correlation peak
Also first->last dhash and a motion-compensated cumulative residual.

Usage: python benchmark/continuity_eval/signals.py EVAL_DIR [FPS] [WIDTH]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image

from scenery_brief_clips.continuity import features_from_image, hamming64, mean_rgb_distance


def frames(path: str, start: float, end: float, fps: float, width: int):
    cap = cv2.VideoCapture(path)
    src_fps = cap.get(cv2.CAP_PROP_FPS) or 30.0
    cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, start) * 1000.0)
    step = max(1, int(round(src_fps / fps)))
    out, idx = [], 0
    n_total = int((end - start) * src_fps)
    while idx <= n_total:
        ok, frame = cap.read()
        if not ok:
            break
        if idx % step == 0:
            h = int(round(frame.shape[0] * width / frame.shape[1]))
            small = cv2.resize(frame, (width, h), interpolation=cv2.INTER_AREA)
            out.append(small)
        idx += 1
    cap.release()
    return out


def pair_signals(a, b) -> dict:
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float32)
    win = cv2.createHanningWindow(ga.shape[::-1], cv2.CV_32F)
    (dx, dy), resp = cv2.phaseCorrelate(ga, gb, win)
    m = np.float32([[1, 0, -dx], [0, 1, -dy]])
    shifted = cv2.warpAffine(gb, m, ga.shape[::-1], flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT, borderValue=-1)
    valid = shifted >= 0
    res = float(np.abs(ga[valid] - shifted[valid]).mean()) if valid.mean() > 0.3 else float(np.abs(ga - gb).mean())
    fa = features_from_image(Image.fromarray(cv2.cvtColor(a, cv2.COLOR_BGR2RGB)))
    fb = features_from_image(Image.fromarray(cv2.cvtColor(b, cv2.COLOR_BGR2RGB)))
    return {
        "dhash": hamming64(fa.dhash, fb.dhash),
        "color": round(mean_rgb_distance(fa.mean_rgb, fb.mean_rgb), 3),
        "mad": round(float(np.abs(ga - gb).mean()), 3),
        "res": round(res, 3),
        "shift": round(float((dx * dx + dy * dy) ** 0.5), 2),
        "resp": round(float(resp), 3),
    }


def compensated_residual(a, b, mask=None) -> float:
    """Mean |a - shift(b)| over pixels in mask after undoing global translation."""
    ga = cv2.cvtColor(a, cv2.COLOR_BGR2GRAY).astype(np.float32)
    gb = cv2.cvtColor(b, cv2.COLOR_BGR2GRAY).astype(np.float32)
    win = cv2.createHanningWindow(ga.shape[::-1], cv2.CV_32F)
    (dx, dy), _resp = cv2.phaseCorrelate(ga, gb, win)
    m = np.float32([[1, 0, -dx], [0, 1, -dy]])
    shifted = cv2.warpAffine(gb, m, ga.shape[::-1], flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT, borderValue=-1)
    valid = shifted >= 0
    if mask is not None:
        valid &= mask
    if valid.mean() < 0.2:
        valid = np.ones_like(valid) if mask is None else mask
        return float(np.abs(ga - gb)[valid].mean())
    return float(np.abs(ga - shifted)[valid].mean())


def dynamic_mask(frames_bgr, min_std: float = 2.0):
    """Pixels that vary across the window; static overlays/logos/letterbox excluded."""
    stack = np.stack([cv2.cvtColor(f, cv2.COLOR_BGR2GRAY).astype(np.float32) for f in frames_bgr])
    mask = stack.std(axis=0) >= min_std
    return mask if mask.mean() >= 0.3 else None


def main(eval_dir: Path, fps: float, width: int) -> None:
    rows = json.loads((eval_dir / "manifest.json").read_text())
    out = {}
    for row in rows:
        fr = frames(row["path"], row["start_s"] - row["k_s"], row["end_s"] - row["k_s"], fps, width)
        pairs = [pair_signals(fr[i], fr[i + 1]) for i in range(len(fr) - 1)]
        first = features_from_image(Image.fromarray(cv2.cvtColor(fr[0], cv2.COLOR_BGR2RGB)))
        last = features_from_image(Image.fromarray(cv2.cvtColor(fr[-1], cv2.COLOR_BGR2RGB)))
        mask = dynamic_mask(fr)
        lag = max(1, int(round(fps)))  # 1 second
        res1m = [round(compensated_residual(fr[i], fr[i + 1], mask), 3) for i in range(len(fr) - 1)]
        reslag = [round(compensated_residual(fr[i], fr[i + lag], mask), 3) for i in range(len(fr) - lag)]
        out[row["id"]] = {
            "n": len(fr),
            "pairs": pairs,
            "res1_masked": res1m,
            "res_lag1s_masked": reslag,
            "mask_frac": round(float(mask.mean()), 3) if mask is not None else 1.0,
            "endpoint_dhash": hamming64(first.dhash, last.dhash),
            "endpoint_color": round(mean_rgb_distance(first.mean_rgb, last.mean_rgb), 3),
        }
        print(row["id"], len(fr), flush=True)
    (eval_dir / f"signals_{int(fps)}fps_v2.json").write_text(json.dumps(out))


if __name__ == "__main__":
    main(Path(sys.argv[1]), float(sys.argv[2]) if len(sys.argv) > 2 else 6.0,
         int(sys.argv[3]) if len(sys.argv) > 3 else 160)
