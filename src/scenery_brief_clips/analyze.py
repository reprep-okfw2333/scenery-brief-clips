from __future__ import annotations

import math
from dataclasses import dataclass


class ConstraintError(ValueError):
    """The run's constraint carries invalid duration settings."""


def validate_duration_settings(*, target_s: float, min_s: float, max_s: float) -> None:
    for name, value in (
        ("target_duration_s", target_s),
        ("duration_min_s", min_s),
        ("duration_max_s", max_s),
    ):
        if not math.isfinite(value):
            raise ConstraintError(f"{name} must be a finite number")
        if value <= 0:
            raise ConstraintError(f"{name} must be positive, got {value}")
    if min_s > max_s:
        raise ConstraintError(f"duration_min_s {min_s} exceeds duration_max_s {max_s}")
    if not (min_s <= target_s <= max_s):
        raise ConstraintError(
            f"target_duration_s {target_s} is outside the {min_s}-{max_s} band"
        )


def duration_settings_from_constraint(constraint: dict) -> tuple[float, float, float]:
    """Resolve and validate (target_s, min_s, max_s) from a constraint payload."""
    defaults = {"target_duration_s": 6.0, "duration_min_s": 4.0, "duration_max_s": 12.0}
    resolved: dict[str, float] = {}
    for key, default in defaults.items():
        value = constraint.get(key)
        if value is None:
            resolved[key] = default
            continue
        try:
            resolved[key] = float(value)
        except (TypeError, ValueError) as exc:
            raise ConstraintError(f"{key} is not a number: {value!r}") from exc
    validate_duration_settings(
        target_s=resolved["target_duration_s"],
        min_s=resolved["duration_min_s"],
        max_s=resolved["duration_max_s"],
    )
    return resolved["target_duration_s"], resolved["duration_min_s"], resolved["duration_max_s"]


@dataclass(frozen=True)
class AnalysisPlan:
    mode: str
    ranges: list[tuple[float, float]]

    @property
    def total_s(self) -> float:
        return sum(end - start for start, end in self.ranges)


@dataclass(frozen=True)
class Excerpt:
    start_s: float
    end_s: float
    source_scene: tuple[float, float]


def _clamp_range(start: float, end: float, duration_s: float) -> tuple[float, float] | None:
    start = max(0.0, start)
    end = min(duration_s, end)
    if end - start <= 0:
        return None
    return (start, end)


def _merge_ranges(ranges: list[tuple[float, float]]) -> list[tuple[float, float]]:
    if not ranges:
        return []
    ordered = sorted(ranges)
    merged = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end:
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _widen_range(
    start: float, end: float, min_len_s: float, duration_s: float
) -> tuple[float, float]:
    """Grow a range around its centre to ``min_len_s``, shifted to stay inside the video."""
    if end - start >= min_len_s:
        return (start, end)
    length = min(min_len_s, duration_s)
    start = (start + end - length) / 2.0
    start = min(max(0.0, start), duration_s - length)
    return (start, start + length)


def _evenly_spaced(items: list, k: int) -> list:
    if k >= len(items):
        return items
    if k <= 1:
        return [items[(len(items) - 1) // 2]]
    return [items[round(i * (len(items) - 1) / (k - 1))] for i in range(k)]


def _cap_ranges_to_budget(
    ranges: list[tuple[float, float]], budget_s: float, min_len_s: float = 0.0
) -> list[tuple[float, float]]:
    if not ranges or budget_s <= 0:
        return []
    lengths = [end - start for start, end in ranges]
    if sum(lengths) <= budget_s:
        return ranges
    if min_len_s > 0 and len(ranges) * min_len_s > budget_s:
        # Shrinking every range to an equal share would leave none long enough
        # for a clip; keep fewer, evenly spaced ranges instead.
        ranges = _evenly_spaced(ranges, max(1, int(budget_s // min_len_s)))
        lengths = [end - start for start, end in ranges]
        if sum(lengths) <= budget_s:
            return ranges

    allocations = [0.0] * len(ranges)
    active = set(range(len(ranges)))
    remaining = budget_s
    while active and remaining > 1e-9:
        share = remaining / len(active)
        small = [index for index in active if lengths[index] <= share]
        if small:
            for index in small:
                allocations[index] = lengths[index]
                remaining -= lengths[index]
                active.remove(index)
            continue
        for index in active:
            allocations[index] = share
        remaining = 0.0

    capped = []
    for (start, end), allocation in zip(ranges, allocations, strict=True):
        if allocation <= 0:
            continue
        length = end - start
        if allocation >= length:
            capped.append((start, end))
            continue
        midpoint = (start + end) / 2.0
        half = allocation / 2.0
        capped.append((midpoint - half, midpoint + half))
    return capped


def _spread_ranges(duration_s: float, budget_s: float, min_len_s: float = 0.0) -> list[tuple[float, float]]:
    if duration_s <= 0 or budget_s <= 0:
        return []
    if budget_s >= duration_s:
        return [(0.0, duration_s)]
    n = max(2, min(8, int(budget_s / 15)))
    if min_len_s > 0:
        n = max(1, min(n, int(budget_s // min_len_s)))
    each = budget_s / n
    span = duration_s - each
    ranges = []
    for i in range(n):
        start = 0.0 if n == 1 else i * span / (n - 1)
        ranges.append((start, start + each))
    return ranges


def analysis_plan(
    duration_s: float,
    windows: list[dict],
    max_analysis_s: float = 600.0,
    pad_s: float = 2.0,
    min_window_s: float = 0.0,
) -> AnalysisPlan:
    """Source ranges to analyze for one video.

    ``min_window_s`` (0 = off, the behavior of runs analyzed before it
    existed) keeps every range long enough to hold a clip of the requested
    length: short tile windows are widened, and a tight budget keeps fewer
    ranges rather than shrinking them all.
    """
    padded: list[tuple[float, float]] = []
    for win in windows:
        start = float(win.get("start_s", 0.0)) - pad_s
        end = float(win.get("end_s", 0.0)) + pad_s
        clamped = _clamp_range(start, end, duration_s)
        if clamped:
            if min_window_s > 0:
                clamped = _widen_range(*clamped, min_window_s, duration_s)
            padded.append(clamped)
    padded = _merge_ranges(padded)
    window_total = sum(end - start for start, end in padded)
    if padded:
        if window_total <= max_analysis_s:
            return AnalysisPlan(mode="windows", ranges=padded)
        return AnalysisPlan(
            mode="windows_capped",
            ranges=_cap_ranges_to_budget(padded, max_analysis_s, min_window_s),
        )
    if duration_s <= max_analysis_s:
        full = _clamp_range(0.0, duration_s, duration_s)
        return AnalysisPlan(mode="full", ranges=[full] if full else [])
    return AnalysisPlan(mode="spread", ranges=_spread_ranges(duration_s, max_analysis_s, min_window_s))


def min_window_for_durations(min_s: float, target_s: float, pad_s: float) -> float:
    """Shortest analysis range for a duration band: room for the shortest clip
    plus the usual pad on both sides, and never shorter than the target."""
    return max(float(min_s) + 2.0 * float(pad_s), float(target_s))


def excerpts_from_scenes(
    scenes: list[tuple[float, float]],
    target_s: float = 6.0,
    min_s: float = 4.0,
    max_s: float = 12.0,
    edge_trim_s: float = 0.3,
) -> list[Excerpt]:
    validate_duration_settings(target_s=target_s, min_s=min_s, max_s=max_s)
    out: list[Excerpt] = []
    for start, end in scenes:
        interior_start = start + edge_trim_s
        interior_end = end - edge_trim_s
        length = interior_end - interior_start
        if length < min_s:
            continue
        if length <= max_s:
            out.append(Excerpt(interior_start, interior_end, (start, end)))
            continue
        mid = (interior_start + interior_end) / 2.0
        half = target_s / 2.0
        excerpt = Excerpt(mid - half, mid + half, (start, end))
        if excerpt.end_s <= excerpt.start_s:
            raise ConstraintError("generated excerpt has a non-positive interval")
        out.append(excerpt)
    return out
