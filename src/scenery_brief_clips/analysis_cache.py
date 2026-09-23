from __future__ import annotations

import hashlib
import math
import re
from pathlib import Path

ANALYSIS_CACHE_POLICY = "v3-video-only-720"
ANALYSIS_MARKER_SCHEMA_VERSION = 1
MEDIA_DURATION_TOLERANCE_S = 3.0
EXPORT_CACHE_POLICY = "v2-export-cap-copyts"
EXPORT_MARKER_SCHEMA_VERSION = 2
EXPORT_DEFAULT_MAX_HEIGHT = 720
EXPORT_CAP_MIN = 720
EXPORT_CAP_MAX = 2160
EXPORT_FLOOR_HEIGHT = 720
EXPORT_ASPECT_TARGET = 16.0 / 9.0
EXPORT_ASPECT_TOLERANCE = 0.01  # absolute tolerance on the 16:9 ratio (~0.56% relative)


def canonical_span_ms(span: tuple[float, float]) -> tuple[int, int]:
    start, end = float(span[0]), float(span[1])
    if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end):
        raise ValueError(f"invalid analysis span: {span}")
    start_ms = round(start * 1000)
    end_ms = round(end * 1000)
    if end_ms <= start_ms:
        raise ValueError(f"analysis span is empty at millisecond precision: {span}")
    return start_ms, end_ms


def canonical_span_seconds(span: tuple[float, float]) -> tuple[float, float]:
    start_ms, end_ms = canonical_span_ms(span)
    return start_ms / 1000.0, end_ms / 1000.0


def safe_video_id(video_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_-]", "_", video_id)
    if safe != video_id:
        suffix = hashlib.sha256(video_id.encode("utf-8")).hexdigest()[:8]
        safe = f"{safe}-{suffix}"
    return safe or "missing-id"


def analysis_cache_path(
    cache_dir: str | Path,
    video_id: str,
    span: tuple[float, float],
    policy: str = ANALYSIS_CACHE_POLICY,
) -> Path:
    start_ms, end_ms = canonical_span_ms(span)
    return Path(cache_dir) / f"{safe_video_id(video_id)}_{start_ms}-{end_ms}_{policy}.mp4"


def analysis_marker_path(dest: str | Path) -> Path:
    path = Path(dest)
    return path.with_suffix(path.suffix + ".complete.json")


def export_cache_path(
    cache_dir: str | Path,
    video_id: str,
    span: tuple[float, float],
    policy: str = EXPORT_CACHE_POLICY,
) -> Path:
    """Part 5 full-resolution acquisition cache path (same keying as analysis)."""
    start_ms, end_ms = canonical_span_ms(span)
    return Path(cache_dir) / f"{safe_video_id(video_id)}_{start_ms}-{end_ms}_{policy}.mp4"


def export_marker_path(dest: str | Path) -> Path:
    path = Path(dest)
    return path.with_suffix(path.suffix + ".complete.json")


def analysis_lock_path(dest: str | Path) -> Path:
    path = Path(dest)
    return path.with_suffix(path.suffix + ".lock")


def sha256_file(path: str | Path) -> str:
    digest = hashlib.sha256()
    with Path(path).open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
