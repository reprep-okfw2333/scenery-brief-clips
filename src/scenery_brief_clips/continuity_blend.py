"""Motion-tolerant continuity detector ("blend", opt-in via continuity_detector).

The legacy gate compares dHash/mean-RGB of 2 fps samples and their endpoints,
so camera pans and marching crowds read as transitions. This detector looks
for the edits themselves, on 6 fps grayscale frames at 160 px:

- cut: at an adjacent pair, the motion-compensated residual (global
  translation undone by phase correlation) is >= CUT_RESIDUAL and >=
  CUT_SPIKE x the median over the scan, or the mean-RGB jump is >= CUT_COLOR.
- dissolve: a middle frame b is well explained by alpha*a + (1-alpha)*c of the
  frames LAG before and after it (alpha in [BLEND_ALPHA_MIN, 1-BLEND_ALPHA_MIN])
  while far from both: min(|b-a|, |b-c|) / (blend residual + 1) >=
  BLEND_SCORE and >= BLEND_SPIKE x the median score over the scan (slow zooms
  and push-ins make b ~ (a+c)/2 everywhere; a dissolve is a burst), and a vs c
  differ (compensated residual >= BLEND_ENDS). Pixels that are static across
  the scan (logos, tickers) are masked out.

The scan reads PAD_S beyond each side of the window (clamped to the file) so
edge dissolves have neighbours; only events inside the window count. Frames
touched by an event are excluded, and the longest clean run of window frames
becomes the keep/trim/reject span (same duration rules as legacy).

Thresholds come from the labeled set in benchmark/continuity_eval/ (66 real
gate decisions, replayed through this module: 0/16 transitions kept, 2/50
continuous rejected (one whip pan, in two runs), 2/50 continuous trimmed;
legacy rejected 25/50 and trimmed 9/50). Change them only with numbers from
that set.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from pathlib import Path

import numpy as np

DETECTOR_NAME = "blend-v1"
SCAN_FPS = 6.0
SCAN_WIDTH = 160
PAD_S = 1.0
CUT_RESIDUAL = 15.0
CUT_SPIKE = 3.5
CUT_COLOR = 35.0
BLEND_LAGS = (2, 3)
BLEND_SCORE = 1.35
BLEND_SPIKE = 1.15  # score / median score over the scan: smooth motion scores high everywhere
BLEND_ENDS = 10.0
BLEND_ALPHA_MIN = 0.15
MASK_MIN_STD = 2.0
FROZEN_DIFF = 0.5  # mean abs luma difference below which a pair counts as frozen
MASK_MIN_FRACTION = 0.3
_EDGE_INSET_S = 0.05


@dataclass(frozen=True)
class ScanFrames:
    """Uniform samples over the padded scan; window = indices first..last."""

    times: list[float]  # local (file) seconds
    frames: list[np.ndarray]  # BGR uint8 at SCAN_WIDTH
    first: int
    last: int


@dataclass(frozen=True)
class Event:
    kind: str  # cut | dissolve
    index: int  # cut: left frame of the pair; dissolve: middle frame
    lag: int  # frames excluded on each side (0 for a cut)


def scan_times(local_start_s: float, local_end_s: float, duration_s: float | None) -> tuple[list[float], int, int]:
    """Grid at SCAN_FPS anchored at the (inset) window start, padded both sides."""
    inset = min(_EDGE_INSET_S, (local_end_s - local_start_s) / 4.0)
    w0 = local_start_s + inset
    w1 = local_end_s - inset
    step = 1.0 / SCAN_FPS
    lo = max(0.0, local_start_s - PAD_S)
    hi = local_end_s + PAD_S
    if duration_s is not None and duration_s > 0:
        hi = min(hi, duration_s - _EDGE_INSET_S)
    n_before = int((w0 - lo) / step + 1e-9)
    n_window = int((w1 - w0) / step + 1e-9) + 1
    n_after = max(0, int((hi - w1) / step + 1e-9))
    times = [w0 + (k - n_before) * step for k in range(n_before + n_window + n_after)]
    return times, n_before, n_before + n_window - 1


def read_scan(video_path: str | Path, local_start_s: float, local_end_s: float) -> ScanFrames:
    """Decode the padded scan sequentially (one seek). Pad frames past EOF are dropped."""
    import cv2

    path = Path(video_path)
    if not path.is_file():
        raise RuntimeError(f"continuity scan missing media {path}")
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"continuity scan could not open {path}")
    try:
        fps = float(cap.get(cv2.CAP_PROP_FPS) or 0.0)
        if fps <= 0:
            raise RuntimeError(f"continuity scan could not read frame rate of {path}")
        n_frames = float(cap.get(cv2.CAP_PROP_FRAME_COUNT) or 0.0)
        duration = n_frames / fps if n_frames > 0 else None
        times, first, last = scan_times(local_start_s, local_end_s, duration)
        frames: list[np.ndarray] = []
        kept_times: list[float] = []
        position = None
        previous = None
        for k, t_s in enumerate(times):
            target = int(max(0.0, t_s) * fps + 0.5)
            if previous is not None and target == previous[0]:
                frames.append(previous[1])
                kept_times.append(t_s)
                continue
            if position is None or target < position:
                cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t_s) * 1000.0)
                position = target
            ok = True
            while ok and position < target:
                ok = cap.grab()
                position += 1
            frame = None
            if ok:
                ok, frame = cap.read()
                position += 1
            if not ok or frame is None:
                if k <= last:
                    raise RuntimeError(f"continuity scan failed to read frame at {t_s:.3f}s in {path}")
                break  # padding past the end of the copy
            if frame.shape[1] > SCAN_WIDTH:
                h = max(1, int(round(frame.shape[0] * SCAN_WIDTH / frame.shape[1])))
                frame = cv2.resize(frame, (SCAN_WIDTH, h), interpolation=cv2.INTER_AREA)
            previous = (target, frame)
            frames.append(frame)
            kept_times.append(t_s)
    finally:
        cap.release()
    return ScanFrames(times=kept_times, frames=frames, first=first, last=last)


def _gray(frame: np.ndarray) -> np.ndarray:
    import cv2

    return cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).astype(np.float32)


def _translation(ga: np.ndarray, gb: np.ndarray) -> np.ndarray:
    """gb warped back by the global translation phase correlation finds; -1 outside."""
    import cv2

    win = cv2.createHanningWindow(ga.shape[::-1], cv2.CV_32F)
    # OpenCV 5.0 phaseCorrelate multiplies the window into its inputs in place;
    # pass copies so frames reused across pairs stay intact.
    (dx, dy), _resp = cv2.phaseCorrelate(ga.copy(), gb.copy(), win)
    m = np.float32([[1, 0, -dx], [0, 1, -dy]])
    return cv2.warpAffine(gb, m, ga.shape[::-1], flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_CONSTANT, borderValue=-1)


def pair_residual(ga: np.ndarray, gb: np.ndarray) -> float:
    """Mean |a - shift(b)| over the overlap (whole frame when the overlap is small)."""
    shifted = _translation(ga, gb)
    valid = shifted >= 0
    if valid.mean() > 0.3:
        return float(np.abs(ga[valid] - shifted[valid]).mean())
    return float(np.abs(ga - gb).mean())


def masked_residual(ga: np.ndarray, gb: np.ndarray, mask: np.ndarray | None) -> float:
    """Compensated residual restricted to dynamic pixels."""
    shifted = _translation(ga, gb)
    valid = shifted >= 0
    if mask is not None:
        valid &= mask
    if valid.mean() < 0.2:
        sel = mask if mask is not None else np.ones_like(valid)
        return float(np.abs(ga - gb)[sel].mean())
    return float(np.abs(ga - shifted)[valid].mean())


def dynamic_mask(gray: list[np.ndarray]) -> np.ndarray | None:
    mask = np.stack(gray).std(axis=0) >= MASK_MIN_STD
    return mask if mask.mean() >= MASK_MIN_FRACTION else None


def blend_records(gray: list[np.ndarray], mask: np.ndarray | None, lag: int, first: int, last: int) -> list[dict]:
    """Blend-fit terms for each middle frame t in first..last with neighbours at +-lag."""
    sel = mask if mask is not None else np.ones_like(gray[0], dtype=bool)
    out = []
    for t in range(max(lag, first), min(len(gray) - lag, last + 1)):
        a, b, c = gray[t - lag][sel], gray[t][sel], gray[t + lag][sel]
        d = a - c
        denom = float((d * d).sum())
        alpha = float(np.clip(((b - c) * d).sum() / denom, 0.0, 1.0)) if denom > 1e-6 else 0.5
        blend = float(np.abs(b - (alpha * a + (1.0 - alpha) * c)).mean())
        near = float(min(np.abs(b - a).mean(), np.abs(b - c).mean()))
        out.append({"t": t, "alpha": alpha, "near": near, "score": near / (blend + 1.0)})
    return out


def cut_records(frames: list[np.ndarray], gray: list[np.ndarray]) -> list[dict]:
    """Per adjacent pair i: compensated residual and mean-RGB jump."""
    from PIL import Image
    import cv2

    from scenery_brief_clips.continuity import features_from_image, mean_rgb_distance

    rgb = [features_from_image(Image.fromarray(cv2.cvtColor(f, cv2.COLOR_BGR2RGB))).mean_rgb for f in frames]
    return [
        {"i": i, "res": pair_residual(gray[i], gray[i + 1]), "color": mean_rgb_distance(rgb[i], rgb[i + 1])}
        for i in range(len(gray) - 1)
    ]


def _moving_median(values: list[float]) -> float:
    """Median of the non-frozen values: a frozen stretch (still title, freeze
    frame) would drag the reference down and let ordinary motion pass as a spike."""
    moving = [v for v in values if v >= FROZEN_DIFF]
    return statistics.median(moving or values)


def detect_events(
    frames: list[np.ndarray],
    first: int,
    last: int,
    *,
    blend_score: float = BLEND_SCORE,
    blend_spike: float = BLEND_SPIKE,
) -> list[Event]:
    """Cuts and dissolves whose frames lie inside window indices first..last."""
    gray = [_gray(f) for f in frames]
    events: list[Event] = []
    if len(gray) < 2:
        return events
    cuts = cut_records(frames, gray)
    median = max(_moving_median([c["res"] for c in cuts]), 1.0)
    for c in cuts:
        if c["i"] < first or c["i"] + 1 > last:
            continue
        if (c["res"] >= CUT_RESIDUAL and c["res"] / median >= CUT_SPIKE) or c["color"] >= CUT_COLOR:
            events.append(Event("cut", c["i"], 0))
    mask = dynamic_mask(gray)
    for lag in BLEND_LAGS:
        # Scored over the whole padded scan so the median sees context.
        records = blend_records(gray, mask, lag, 0, len(gray) - 1)
        if not records:
            continue
        moving = [r["score"] for r in records if r["near"] >= FROZEN_DIFF]  # skip frozen middles
        typical = max(statistics.median(moving or [r["score"] for r in records]), 0.5)
        for r in records:
            t = r["t"]
            if t < first or t > last or not (BLEND_ALPHA_MIN <= r["alpha"] <= 1.0 - BLEND_ALPHA_MIN):
                continue
            if r["score"] < blend_score or r["score"] / typical < blend_spike:
                continue
            if masked_residual(gray[t - lag], gray[t + lag], mask) < BLEND_ENDS:
                continue
            events.append(Event("dissolve", t, lag))
    return events


def clean_runs(first: int, last: int, events: list[Event]) -> list[tuple[int, int]]:
    """Maximal inclusive runs of window frames not touched by an event.

    A cut at pair (i, i+1) excludes both samples: a fast dissolve (a few source
    frames) can already blend sample i, as seen in the labeled set. A dissolve
    at t with lag L excludes t-L..t+L (the fit endpoints too, as a margin).
    """
    excluded: set[int] = set()
    for ev in events:
        if ev.kind == "cut":
            excluded.update((ev.index, ev.index + 1))
        else:
            excluded.update(range(ev.index - ev.lag, ev.index + ev.lag + 1))
    runs: list[tuple[int, int]] = []
    start = None
    for i in range(first, last + 1):
        if i in excluded:
            if start is not None:
                runs.append((start, i - 1))
            start = None
        elif start is None:
            start = i
    if start is not None:
        runs.append((start, last))
    return runs
