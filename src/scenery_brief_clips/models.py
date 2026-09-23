from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class RunLimits:
    max_search_results: int = 20
    max_metadata_fetches: int = 30
    max_bytes: int = 0
    max_seconds: int = 120
    sleep_s: float = 2.0


@dataclass(frozen=True)
class Constraint:
    theme_text: str
    min_width: int = 1280
    min_height: int = 720
    aspect_min: float = 1.70
    aspect_max: float = 1.86
    n_clips: int = 20
    target_duration_s: float = 6.0
    duration_min_s: float = 4.0
    duration_max_s: float = 12.0
    geo_requirement: str = "none"
    allow_download: bool = False
    visual_positives: tuple[str, ...] = ()
    visual_negatives: tuple[str, ...] = (
        "people",
        "faces",
        "text",
        "maps",
        "logos",
        "watermarks",
        "vlog",
    )
    limits: RunLimits = field(default_factory=RunLimits)


@dataclass(frozen=True)
class Candidate:
    video_id: str
    title: str
    duration_s: float | None
    width: int
    height: int
    fps: float | None
    aspect: float
    format_id: str
    watch_url: str
    reason_kept: str
    duration_hint: str


@dataclass(frozen=True)
class Reject:
    video_id: str
    title: str | None
    reason: str
