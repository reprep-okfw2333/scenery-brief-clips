"""Deterministic continuity gate for Part 3 excerpt windows.

PySceneDetect misses soft dissolves. Before an excerpt becomes a shortlist
candidate, sample the analysis copy cheaply (~2 fps) and either keep, trim to
the longest stable subspan that still meets duration_min_s, or reject.

Stability uses dHash Hamming (structure) OR mean-RGB distance (colour), so
flat-colour cuts and textured scene changes both fail closed.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from PIL import Image

from scenery_brief_clips.analyze import ConstraintError, Excerpt, validate_duration_settings
from scenery_brief_clips.shortlist import hamming64, image_dhash


DEFAULT_CONTINUITY_SAMPLE_FPS = 2.0
DEFAULT_CONTINUITY_MAX_ADJACENT_HAMMING = 18
DEFAULT_CONTINUITY_MAX_ENDPOINT_HAMMING = 22
DEFAULT_CONTINUITY_MAX_ADJACENT_COLOR = 28.0
DEFAULT_CONTINUITY_MAX_ENDPOINT_COLOR = 35.0
# Review defense-in-depth: flag moments whose strip endpoints diverge this far.
DEFAULT_REVIEW_CONTINUITY_SUSPECT_HAMMING = 22
DEFAULT_REVIEW_CONTINUITY_SUSPECT_COLOR = 35.0
_SAMPLE_EDGE_INSET_S = 0.05


@dataclass(frozen=True)
class ContinuitySettings:
    enabled: bool = True
    sample_fps: float = DEFAULT_CONTINUITY_SAMPLE_FPS
    max_adjacent_hamming: int = DEFAULT_CONTINUITY_MAX_ADJACENT_HAMMING
    max_endpoint_hamming: int = DEFAULT_CONTINUITY_MAX_ENDPOINT_HAMMING
    max_adjacent_color: float = DEFAULT_CONTINUITY_MAX_ADJACENT_COLOR
    max_endpoint_color: float = DEFAULT_CONTINUITY_MAX_ENDPOINT_COLOR

    def validated(self) -> ContinuitySettings:
        if not isinstance(self.enabled, bool):
            raise ConstraintError("continuity_enabled must be a boolean")
        fps = float(self.sample_fps)
        if not math.isfinite(fps) or fps <= 0:
            raise ConstraintError("continuity_sample_fps must be finite and greater than 0")
        for name, value in (
            ("continuity_max_adjacent_hamming", self.max_adjacent_hamming),
            ("continuity_max_endpoint_hamming", self.max_endpoint_hamming),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 0 or value > 64:
                raise ConstraintError(f"{name} must be an integer in 0..64")
        for name, value in (
            ("continuity_max_adjacent_color", self.max_adjacent_color),
            ("continuity_max_endpoint_color", self.max_endpoint_color),
        ):
            value = float(value)
            if not math.isfinite(value) or value < 0 or value > 255:
                raise ConstraintError(f"{name} must be a finite number in 0..255")
        return ContinuitySettings(
            enabled=self.enabled,
            sample_fps=fps,
            max_adjacent_hamming=int(self.max_adjacent_hamming),
            max_endpoint_hamming=int(self.max_endpoint_hamming),
            max_adjacent_color=float(self.max_adjacent_color),
            max_endpoint_color=float(self.max_endpoint_color),
        )

    def as_manifest(self) -> dict:
        return {
            "continuity_enabled": self.enabled,
            "continuity_sample_fps": self.sample_fps,
            "continuity_max_adjacent_hamming": self.max_adjacent_hamming,
            "continuity_max_endpoint_hamming": self.max_endpoint_hamming,
            "continuity_max_adjacent_color": self.max_adjacent_color,
            "continuity_max_endpoint_color": self.max_endpoint_color,
        }


@dataclass(frozen=True)
class FrameFeatures:
    dhash: int
    mean_rgb: tuple[float, float, float]


@dataclass(frozen=True)
class ContinuityDecision:
    action: str  # keep | trim | reject
    start_s: float
    end_s: float
    reason: str | None
    max_adjacent_hamming: int
    endpoint_hamming: int
    max_adjacent_color: float
    endpoint_color: float
    samples: int
    original_start_s: float
    original_end_s: float

    def as_excerpt_meta(self) -> dict:
        meta = {
            "action": self.action,
            "max_adjacent_hamming": self.max_adjacent_hamming,
            "endpoint_hamming": self.endpoint_hamming,
            "max_adjacent_color": round(self.max_adjacent_color, 3),
            "endpoint_color": round(self.endpoint_color, 3),
            "samples": self.samples,
        }
        if self.action == "trim":
            meta["original_start_s"] = self.original_start_s
            meta["original_end_s"] = self.original_end_s
        return meta

    def as_reject_record(self) -> dict:
        return {
            "start_s": self.original_start_s,
            "end_s": self.original_end_s,
            "reason": self.reason or "continuity reject",
            "max_adjacent_hamming": self.max_adjacent_hamming,
            "endpoint_hamming": self.endpoint_hamming,
            "max_adjacent_color": round(self.max_adjacent_color, 3),
            "endpoint_color": round(self.endpoint_color, 3),
            "samples": self.samples,
        }


def mean_rgb(image: Image.Image) -> tuple[float, float, float]:
    from PIL import ImageStat

    small = image.convert("RGB").resize((16, 16), Image.Resampling.BILINEAR)
    means = ImageStat.Stat(small).mean
    return (float(means[0]), float(means[1]), float(means[2]))


def mean_rgb_distance(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    """Max per-channel absolute difference on 0..255 mean RGB."""
    return max(abs(x - y) for x, y in zip(a, b, strict=True))


def features_from_image(image: Image.Image) -> FrameFeatures:
    return FrameFeatures(dhash=image_dhash(image), mean_rgb=mean_rgb(image))


def features_from_path(path: str | Path) -> FrameFeatures:
    with Image.open(path) as image:
        return features_from_image(image)


def pair_distances(a: FrameFeatures, b: FrameFeatures) -> tuple[int, float]:
    return hamming64(a.dhash, b.dhash), mean_rgb_distance(a.mean_rgb, b.mean_rgb)


def continuity_sample_times(start_s: float, end_s: float, fps: float) -> list[float]:
    """Inclusive-endpoint times at roughly `fps` samples per second (inset from EOF)."""
    start_s = float(start_s)
    end_s = float(end_s)
    length = end_s - start_s
    if not math.isfinite(length) or length <= 0:
        raise ConstraintError("continuity window has a non-positive interval")
    fps = float(fps)
    if not math.isfinite(fps) or fps <= 0:
        raise ConstraintError("continuity_sample_fps must be finite and greater than 0")
    inset = min(_SAMPLE_EDGE_INSET_S, length / 4.0)
    first = start_s + inset
    last = end_s - inset
    span = last - first
    n = max(2, int(math.floor(span * fps)) + 1)
    if n == 2:
        return [first, last]
    step = span / (n - 1)
    return [first + i * step for i in range(n)]


def window_distances(features: list[FrameFeatures]) -> tuple[int, int, float, float]:
    """Return (max_adj_hamming, endpoint_hamming, max_adj_color, endpoint_color)."""
    if len(features) < 2:
        raise ConstraintError("continuity scan requires at least two samples")
    max_adj_h = 0
    max_adj_c = 0.0
    for left, right in zip(features, features[1:]):
        h, c = pair_distances(left, right)
        max_adj_h = max(max_adj_h, h)
        max_adj_c = max(max_adj_c, c)
    end_h, end_c = pair_distances(features[0], features[-1])
    return max_adj_h, end_h, max_adj_c, end_c


def _pair_unstable(
    a: FrameFeatures,
    b: FrameFeatures,
    *,
    max_hamming: int,
    max_color: float,
) -> bool:
    h, c = pair_distances(a, b)
    return h > max_hamming or c > max_color


def longest_stable_span(
    times: list[float],
    features: list[FrameFeatures],
    *,
    max_adjacent_hamming: int,
    max_endpoint_hamming: int,
    max_adjacent_color: float,
    max_endpoint_color: float,
) -> tuple[int, int] | None:
    """Inclusive indices of the longest stable subspan; ties prefer earlier start."""
    if len(times) != len(features) or len(features) < 2:
        return None
    best: tuple[float, int, int] | None = None
    n = len(features)
    for i in range(n):
        for j in range(i + 1, n):
            if _pair_unstable(
                features[j - 1],
                features[j],
                max_hamming=max_adjacent_hamming,
                max_color=max_adjacent_color,
            ):
                break
            if _pair_unstable(
                features[i],
                features[j],
                max_hamming=max_endpoint_hamming,
                max_color=max_endpoint_color,
            ):
                break
            length = times[j] - times[i]
            if best is None or length > best[0] or (length == best[0] and i < best[1]):
                best = (length, i, j)
    if best is None:
        return None
    return best[1], best[2]


def decide_from_samples(
    *,
    start_s: float,
    end_s: float,
    times: list[float],
    features: list[FrameFeatures],
    target_s: float,
    min_s: float,
    max_s: float,
    max_adjacent_hamming: int,
    max_endpoint_hamming: int,
    max_adjacent_color: float,
    max_endpoint_color: float,
) -> ContinuityDecision:
    """Pure decision given sample times/features in the candidate window."""
    validate_duration_settings(target_s=target_s, min_s=min_s, max_s=max_s)
    if len(times) != len(features) or len(features) < 2:
        raise ConstraintError("continuity scan requires at least two samples")
    max_adj_h, end_h, max_adj_c, end_c = window_distances(features)
    original_start = float(start_s)
    original_end = float(end_s)

    stable = (
        max_adj_h <= max_adjacent_hamming
        and end_h <= max_endpoint_hamming
        and max_adj_c <= max_adjacent_color
        and end_c <= max_endpoint_color
    )
    if stable:
        return ContinuityDecision(
            action="keep",
            start_s=original_start,
            end_s=original_end,
            reason=None,
            max_adjacent_hamming=max_adj_h,
            endpoint_hamming=end_h,
            max_adjacent_color=max_adj_c,
            endpoint_color=end_c,
            samples=len(features),
            original_start_s=original_start,
            original_end_s=original_end,
        )

    span = longest_stable_span(
        times,
        features,
        max_adjacent_hamming=max_adjacent_hamming,
        max_endpoint_hamming=max_endpoint_hamming,
        max_adjacent_color=max_adjacent_color,
        max_endpoint_color=max_endpoint_color,
    )
    if span is None:
        return ContinuityDecision(
            action="reject",
            start_s=original_start,
            end_s=original_end,
            reason="no stable continuity subspan",
            max_adjacent_hamming=max_adj_h,
            endpoint_hamming=end_h,
            max_adjacent_color=max_adj_c,
            endpoint_color=end_c,
            samples=len(features),
            original_start_s=original_start,
            original_end_s=original_end,
        )

    i, j = span
    stable_start = float(times[i])
    stable_end = float(times[j])
    stable_len = stable_end - stable_start
    if stable_len < min_s:
        return ContinuityDecision(
            action="reject",
            start_s=original_start,
            end_s=original_end,
            reason=f"stable continuity subspan {stable_len:.3f}s below duration_min_s {min_s}",
            max_adjacent_hamming=max_adj_h,
            endpoint_hamming=end_h,
            max_adjacent_color=max_adj_c,
            endpoint_color=end_c,
            samples=len(features),
            original_start_s=original_start,
            original_end_s=original_end,
        )

    if stable_len <= max_s:
        new_start, new_end = stable_start, stable_end
    else:
        mid = (stable_start + stable_end) / 2.0
        half = target_s / 2.0
        new_start = mid - half
        new_end = mid + half
        if new_start < stable_start:
            new_start = stable_start
            new_end = stable_start + target_s
        if new_end > stable_end:
            new_end = stable_end
            new_start = stable_end - target_s

    seg = features[i : j + 1]
    seg_adj_h, seg_end_h, seg_adj_c, seg_end_c = window_distances(seg)
    return ContinuityDecision(
        action="trim",
        start_s=new_start,
        end_s=new_end,
        reason="trimmed to stable continuity subspan",
        max_adjacent_hamming=seg_adj_h,
        endpoint_hamming=seg_end_h,
        max_adjacent_color=seg_adj_c,
        endpoint_color=seg_end_c,
        samples=len(features),
        original_start_s=original_start,
        original_end_s=original_end,
    )


def sample_features_from_video(
    video_path: str | Path,
    local_start_s: float,
    local_end_s: float,
    *,
    fps: float = DEFAULT_CONTINUITY_SAMPLE_FPS,
) -> tuple[list[float], list[FrameFeatures]]:
    """Sample ~fps frames from an analysis copy; times are local to the file."""
    import cv2

    path = Path(video_path)
    if not path.is_file():
        raise RuntimeError(f"continuity scan missing media {path}")
    times = continuity_sample_times(local_start_s, local_end_s, fps)
    cap = cv2.VideoCapture(str(path))
    if not cap.isOpened():
        raise RuntimeError(f"continuity scan could not open {path}")
    features: list[FrameFeatures] = []
    # Optional decode downscale (SCENERY_CONTINUITY_DECODE_WIDTH). Default 160; set 0 for full-frame legacy decode.
    # Speeds seek/decode; dhash/mean_rgb already shrink further. Width 0 preserves legacy.
    import os
    try:
        decode_width = int(os.environ.get("SCENERY_CONTINUITY_DECODE_WIDTH", "160") or "0")
    except ValueError:
        decode_width = 0
    try:
        for t_s in times:
            cap.set(cv2.CAP_PROP_POS_MSEC, max(0.0, t_s) * 1000.0)
            ok, frame = cap.read()
            if not ok or frame is None:
                raise RuntimeError(f"continuity scan failed to read frame at {t_s:.3f}s in {path}")
            if decode_width > 0 and frame.shape[1] > decode_width:
                new_h = max(1, int(round(frame.shape[0] * (decode_width / frame.shape[1]))))
                frame = cv2.resize(frame, (decode_width, new_h), interpolation=cv2.INTER_AREA)
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            features.append(features_from_image(Image.fromarray(rgb)))
    finally:
        cap.release()
    return times, features


# Backwards-compatible alias used by tests that only care about dHash sequences.
def sample_dhashes_from_video(
    video_path: str | Path,
    local_start_s: float,
    local_end_s: float,
    *,
    fps: float = DEFAULT_CONTINUITY_SAMPLE_FPS,
) -> tuple[list[float], list[int]]:
    times, features = sample_features_from_video(
        video_path, local_start_s, local_end_s, fps=fps
    )
    return times, [f.dhash for f in features]


def gate_excerpt(
    excerpt: Excerpt,
    *,
    video_path: str | Path,
    analysis_span: tuple[float, float],
    target_s: float,
    min_s: float,
    max_s: float,
    settings: ContinuitySettings,
) -> ContinuityDecision:
    """Scan one candidate excerpt on its analysis copy and decide keep/trim/reject."""
    settings = settings.validated()
    offset = float(analysis_span[0])
    local_start = float(excerpt.start_s) - offset
    local_end = float(excerpt.end_s) - offset
    if local_end <= local_start:
        raise ConstraintError("excerpt falls outside analysis span for continuity scan")
    local_times, features = sample_features_from_video(
        video_path,
        local_start,
        local_end,
        fps=settings.sample_fps,
    )
    source_times = [t + offset for t in local_times]
    return decide_from_samples(
        start_s=excerpt.start_s,
        end_s=excerpt.end_s,
        times=source_times,
        features=features,
        target_s=target_s,
        min_s=min_s,
        max_s=max_s,
        max_adjacent_hamming=settings.max_adjacent_hamming,
        max_endpoint_hamming=settings.max_endpoint_hamming,
        max_adjacent_color=settings.max_adjacent_color,
        max_endpoint_color=settings.max_endpoint_color,
    )


def continuity_settings_from_config(config: dict | None) -> ContinuitySettings:
    config = config or {}
    return ContinuitySettings(
        enabled=bool(config.get("continuity_enabled", True)),
        sample_fps=float(config.get("continuity_sample_fps", DEFAULT_CONTINUITY_SAMPLE_FPS)),
        max_adjacent_hamming=int(
            config.get(
                "continuity_max_adjacent_hamming",
                DEFAULT_CONTINUITY_MAX_ADJACENT_HAMMING,
            )
        ),
        max_endpoint_hamming=int(
            config.get(
                "continuity_max_endpoint_hamming",
                DEFAULT_CONTINUITY_MAX_ENDPOINT_HAMMING,
            )
        ),
        max_adjacent_color=float(
            config.get(
                "continuity_max_adjacent_color",
                DEFAULT_CONTINUITY_MAX_ADJACENT_COLOR,
            )
        ),
        max_endpoint_color=float(
            config.get(
                "continuity_max_endpoint_color",
                DEFAULT_CONTINUITY_MAX_ENDPOINT_COLOR,
            )
        ),
    ).validated()
