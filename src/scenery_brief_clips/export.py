"""Part 5 — HD export: turn a verified run's shortlist into final clip files.

Design and done criteria: docs/EXPORT.md.

Flow (plan-first, fail-closed):
  1. Load the run's shortlist (selected moments only) and bind it to the
     analysis generation and to the reviewed/labelled material; malformed,
     changed, or duplicate inputs fail closed before anything is written or
     downloaded.
  2. Build an immutable export plan: per-moment acquisition span ("acquire
     slightly wide", clamped to >= 0 and to the source duration) and the
     resolved rendition -- the largest video-only rendition at or under the
     export cap (default 720p), never below the 720p floor, never scaled or
     upscaled, native 16:9, strictly deterministic.
  3. Acquire each planned section through the run-authorized acquisition path
     (video-only stream copy with absolute timestamps via copyts; the source
     -> local mapping is the first packet PTS) and require that the section
     covers the exact clip interval.
  4. Trim the interior with ONE controlled libx264 encode (half-open
     [start, end) frame semantics; no padding, no substitutes).
  5. Publish out/<theme>/{plan.json, clips/*.mp4, manifest.json} with fsynced
     staged renames, then write the run-dir pointer (run_dir/export.json)
     last; the published generation contains exactly the listed clips.

Honesty rules: a moment that cannot be delivered at full quality becomes a
recorded failure with a machine-readable reason; nothing is ever downgraded,
upscaled, padded, or swapped in.
"""

from __future__ import annotations

import fcntl
import json
import math
import os
import re
import shutil
import subprocess
import uuid
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path

from scenery_brief_clips.analysis_cache import (
    EXPORT_ASPECT_TARGET,
    EXPORT_ASPECT_TOLERANCE,
    EXPORT_CAP_MAX,
    EXPORT_CAP_MIN,
    EXPORT_DEFAULT_MAX_HEIGHT,
    EXPORT_FLOOR_HEIGHT,
    safe_video_id,
    sha256_file,
)
from scenery_brief_clips.run_lock import exclusive_run_lock
from scenery_brief_clips.shortlist import (
    SHORTLIST_SCHEMA_VERSION,
    ShortlistInputError,
    ShortlistStaleError,
    parse_run_json,
    read_run_bytes,
)
from scenery_brief_clips.store import write_json_atomic
from scenery_brief_clips.yt import (
    ExportMediaError,
    ExportSpec,
    YtDlp,
    expected_frames,
    probe_export_coverage,
)

EXPORT_SCHEMA_VERSION = 2
EXPORT_POLICY = "v2-export-cap-sections"
EXPORT_RECIPE = "x264-crf17-medium-v1"
MARGIN_S = 2.0
MAX_HEIGHT_DEFAULT = EXPORT_DEFAULT_MAX_HEIGHT
FLOOR_HEIGHT = EXPORT_FLOOR_HEIGHT
CODEC_PREFERENCE = ("avc1", "av01", "vp09", "vp9")
ENCODE_PRESET = "medium"
ENCODE_CRF = 17
ENCODE_TIMEOUT_S = 2400
COVERAGE_EPS_MS = 1
CLIP_DURATION_TOLERANCE_MS = 50
EXPORT_ATTEMPTS = 2
POINTER_SCHEMA_VERSION = 2
MIN_FREE_DISK_BYTES = 2_000_000_000
ASPECT_TARGET = EXPORT_ASPECT_TARGET
ASPECT_TOLERANCE = EXPORT_ASPECT_TOLERANCE
_FORMAT_ID_RE = re.compile(r"[A-Za-z0-9._-]{1,64}")


class ExportError(ValueError):
    """Invalid export request or inputs (CLI exit 2)."""


class ExportStaleError(ValueError):
    """The run must be re-generated before exporting (CLI exit 1)."""


def source_ms(seconds) -> int:
    return int(round(float(seconds) * 1000))


def slug_theme(text: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(text or "").lower()).strip("-")
    slug = slug[:60].rstrip("-")
    return slug or "theme"


def clip_name(video_id: str, excerpt_index: int, start_ms: int, end_ms: int) -> str:
    return f"{safe_video_id(video_id)}_e{int(excerpt_index)}_{int(start_ms)}-{int(end_ms)}.mp4"


def expected_clip_frames(start_s: float, end_s: float, fps_num: int, fps_den: int) -> int:
    """Frames kept by the half-open trim [start, end) on a frame grid of fps."""
    return expected_frames(start_s, end_s, fps_num, fps_den)


def coverage_problem(start_ms: int, end_ms: int, k_ms: int, last_end_ms: int) -> str | None:
    """Millisecond-precision coverage contract (no clamping, no slack).

    k_ms is the source time of the acquisition's local 0 (first packet PTS);
    local times derive from it exactly. If the acquisition starts after the
    clip start (beyond 1ms timestamp precision) the beginning would be
    missing, so the moment fails instead of silently shortening the interval.
    """
    if k_ms > start_ms + COVERAGE_EPS_MS:
        return (
            f"acquisition starts at source {k_ms}ms, after the clip start {start_ms}ms; "
            "beginning would be missing"
        )
    if last_end_ms < end_ms - COVERAGE_EPS_MS:
        return (
            f"acquisition ends at source {last_end_ms}ms, before the clip end {end_ms}ms; "
            "final content missing"
        )
    return None


def resolve_max_height(config: dict | None) -> int:
    """Resolve and validate the export cap (config key ``export_max_height``)."""
    if not isinstance(config, dict) or "export_max_height" not in config:
        return MAX_HEIGHT_DEFAULT
    value = config["export_max_height"]
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or not (EXPORT_CAP_MIN <= value <= EXPORT_CAP_MAX)
    ):
        raise ExportError(
            f"export_max_height must be an integer between {EXPORT_CAP_MIN} and "
            f"{EXPORT_CAP_MAX}, got {value!r}"
        )
    return value


def _strict_dimension(value) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def resolve_export_spec(
    formats, recorded_width, recorded_height, max_height
) -> tuple[dict | None, str | None]:
    """Pick the largest video-only rendition at or under the export cap.

    Policy (docs/EXPORT.md): never below the 720p floor, never above the cap,
    never scaled or upscaled, native 16:9, strict integer dimensions, explicit
    video-only metadata (``acodec == "none"``), and a deterministic tie-break
    (codec preference, then higher fps, then ascending format id) so shuffled
    metadata can never change a plan. When the top height tier cannot satisfy
    policy the moment fails -- it is never silently delivered at a lower
    height, and the recorded source dimensions are an upper bound.
    """
    if (
        isinstance(max_height, bool)
        or not isinstance(max_height, int)
        or not (EXPORT_CAP_MIN <= max_height <= EXPORT_CAP_MAX)
    ):
        return None, "cap_invalid"
    if not isinstance(formats, list):
        return None, "metadata_invalid"
    recorded_w = _strict_dimension(recorded_width)
    recorded_h = _strict_dimension(recorded_height)
    if recorded_w is None or recorded_h is None:
        return None, "recorded_dimensions_missing"

    candidates: list[dict] = []
    for fmt in formats:
        if not isinstance(fmt, dict):
            continue
        codec = str(fmt.get("vcodec") or "none")
        if codec in ("none", "None", ""):
            continue
        if fmt.get("acodec") != "none":
            continue  # video-only renditions only, proven by explicit metadata
        width = _strict_dimension(fmt.get("width"))
        height = _strict_dimension(fmt.get("height"))
        if width is None or height is None or height > max_height:
            continue
        candidates.append(fmt)
    if not candidates:
        return None, "no_rendition_at_target"

    best_height = max(int(fmt["height"]) for fmt in candidates)
    if best_height < FLOOR_HEIGHT:
        return None, "below_720_floor"
    tier = [fmt for fmt in candidates if int(fmt["height"]) == best_height]

    aspect_ok = [
        fmt
        for fmt in tier
        if abs(int(fmt["width"]) / best_height - ASPECT_TARGET) <= ASPECT_TOLERANCE
    ]
    if not aspect_ok:
        return None, "aspect_unsupported"
    identified = [
        fmt for fmt in aspect_ok if _FORMAT_ID_RE.fullmatch(str(fmt.get("format_id") or ""))
    ]
    if not identified:
        return None, "bad_format_id"
    recorded_ok = [
        fmt
        for fmt in identified
        if int(fmt["width"]) <= recorded_w and best_height <= recorded_h
    ]
    if not recorded_ok:
        return None, "rendition_exceeds_recorded"

    def _order(fmt: dict):
        codec = str(fmt.get("vcodec") or "").lower()
        preference = next(
            (index for index, pref in enumerate(CODEC_PREFERENCE) if codec.startswith(pref)),
            len(CODEC_PREFERENCE),
        )
        fps = fmt.get("fps")
        fps_value = (
            float(fps) if isinstance(fps, (int, float)) and not isinstance(fps, bool) else 0.0
        )
        return (preference, -fps_value, str(fmt.get("format_id")))

    chosen = min(recorded_ok, key=_order)
    return {
        "format_id": str(chosen["format_id"]),
        "width": int(chosen["width"]),
        "height": best_height,
        "codec": str(chosen["vcodec"]),
    }, None


def clip_media_problem(
    info: dict,
    *,
    local_start_s: float,
    local_end_s: float,
    width: int,
    height: int,
    max_height: int,
) -> tuple[str, str] | None:
    """The shared final-media contract (fresh encode, reuse, and verify).

    Returns (code, message) for the first violated rule, or None. Local times
    are millisecond-derived in every caller, so the three paths agree exactly.
    """
    actual_w = int(info.get("width") or 0)
    actual_h = int(info.get("height") or 0)
    if (actual_w, actual_h) != (int(width), int(height)):
        return ("dimension_mismatch", f"clip is {actual_w}x{actual_h}, planned {width}x{height}")
    if (
        isinstance(max_height, bool)
        or not isinstance(max_height, int)
        or not (EXPORT_CAP_MIN <= max_height <= EXPORT_CAP_MAX)
    ):
        return ("cap_invalid", f"invalid export cap {max_height!r}")
    if actual_h < FLOOR_HEIGHT or actual_h > max_height:
        return (
            "quality_band",
            f"height {actual_h} is outside the {FLOOR_HEIGHT}p..{max_height}p band",
        )
    if actual_h and abs(actual_w / actual_h - ASPECT_TARGET) > ASPECT_TOLERANCE:
        return ("aspect_unsupported", f"aspect {actual_w / actual_h:.4f} is not 16:9")
    fps_num = int(info.get("fps_num") or 0)
    fps_den = int(info.get("fps_den") or 0)
    if fps_num <= 0 or fps_den <= 0:
        return ("timestamps_invalid", "clip has no usable frame rate")
    expected = expected_clip_frames(local_start_s, local_end_s, fps_num, fps_den)
    if int(info.get("n_frames") or 0) != expected:
        return ("trim_mismatch", f"clip has {info.get('n_frames')} frames, expected {expected}")
    last_end_ms = source_ms(info.get("last_end_s") or 0.0)
    planned_ms = source_ms(float(local_end_s) - float(local_start_s))
    if abs(last_end_ms - planned_ms) > CLIP_DURATION_TOLERANCE_MS:
        return ("duration_mismatch", f"clip ends at {last_end_ms}ms, planned {planned_ms}ms")
    frame_s = fps_den / fps_num
    if float(info.get("max_gap_s") or 0.0) > frame_s + 1e-3:
        return (
            "gap_exceeded",
            f"frame gap {float(info.get('max_gap_s')):.3f}s exceeds the frame interval {frame_s:.3f}s",
        )
    if abs(float(info.get("first_pts_s") or 0.0)) > 0.05:
        return (
            "timeline_origin",
            f"clip timeline starts at {info.get('first_pts_s')}s, expected 0",
        )
    return None


def _metadata_path(root: Path, video_id: str) -> Path:
    safe = video_id.replace("/", "_").replace("\\", "_")
    return root / "data" / "cache" / "metadata" / f"{safe}.json"


def _load_inputs(run_dir: Path, root: Path) -> dict:
    if not run_dir.is_dir():
        raise ExportError(f"run dir not found: {run_dir}")
    required = {
        "shortlist.json": run_dir / "shortlist.json",
        "constraint.json": run_dir / "constraint.json",
        "ranked.json": run_dir / "ranked.json",
        "analysis_manifest.json": run_dir / "analysis_manifest.json",
    }
    if not required["shortlist.json"].is_file():
        raise ExportError(f"shortlist.json not found in {run_dir}; run shortlist-apply first")
    loaded_bytes: dict[str, bytes] = {}
    for name, path in required.items():
        try:
            loaded_bytes[name] = read_run_bytes(path, name)
        except ShortlistStaleError as exc:
            raise ExportStaleError(str(exc)) from exc
        except ShortlistInputError as exc:
            raise ExportError(str(exc)) from exc

    shortlist = parse_run_json(loaded_bytes["shortlist.json"], "shortlist.json")
    constraint = parse_run_json(loaded_bytes["constraint.json"], "constraint.json")
    ranked = parse_run_json(loaded_bytes["ranked.json"], "ranked.json")
    manifest = parse_run_json(loaded_bytes["analysis_manifest.json"], "analysis_manifest.json")

    if not isinstance(shortlist, dict):
        raise ExportStaleError("shortlist.json is not a JSON object; run shortlist-apply first")
    if shortlist.get("schema_version") != SHORTLIST_SCHEMA_VERSION:
        raise ExportStaleError("shortlist.json has an unsupported schema; run shortlist-apply first")
    bindings = shortlist.get("bindings") or {}
    if not isinstance(bindings, dict):
        raise ExportStaleError("shortlist.json bindings are malformed; run shortlist-apply first")
    if not isinstance(manifest, dict):
        raise ExportStaleError("analysis_manifest.json is not a JSON object; run analyze first")
    if not isinstance(constraint, dict):
        raise ExportStaleError("constraint.json is not a JSON object; the run data is broken")
    if not isinstance(ranked, list):
        raise ExportStaleError("ranked.json is not a list of rows; the run data is broken")

    generation_id = manifest.get("generation_id")
    if not isinstance(generation_id, str) or not generation_id:
        raise ExportStaleError("analysis_manifest.json has no generation id; run analyze first")
    if bindings.get("generation_id") != generation_id:
        raise ExportStaleError(
            "shortlist.json is stale for the current analysis generation; run shortlist-apply first"
        )
    if bindings.get("excerpts_sha256") != manifest.get("excerpts_sha256"):
        raise ExportStaleError(
            "shortlist.json does not match the current excerpts generation; run shortlist-apply first"
        )

    # The loaded run inputs must match the hashes the analysis manifest records.
    for name, manifest_key in (
        ("ranked.json", "ranked_sha256"),
        ("constraint.json", "constraint_sha256"),
    ):
        expected = manifest.get(manifest_key)
        if not isinstance(expected, str) or len(expected) != 64:
            raise ExportStaleError(f"analysis_manifest.json has no valid {manifest_key}; run analyze first")
        if sha256_file(required[name]) != expected:
            raise ExportStaleError(
                f"{name} changed after analysis; re-run analyze before exporting"
            )

    # The reviewed material and the labels the shortlist was derived from
    # must still be the current ones (re-derivation itself is verify's job).
    review_expected = bindings.get("review_sha256")
    if not isinstance(review_expected, str) or len(review_expected) != 64:
        raise ExportStaleError("shortlist.json has no review binding; run shortlist-apply first")
    review_path = run_dir / "review.json"
    if not review_path.is_file() or sha256_file(review_path) != review_expected:
        raise ExportStaleError(
            "review material changed after the shortlist was built; re-run shortlist-review and shortlist-apply"
        )
    labels_expected = bindings.get("labels_sha256")
    labels_rel = bindings.get("labels_path")
    if not isinstance(labels_expected, str) or len(labels_expected) != 64 or not isinstance(labels_rel, str):
        raise ExportStaleError("shortlist.json has no labels binding; run shortlist-apply first")
    labels_path = (run_dir / labels_rel).resolve()
    if not labels_path.is_file() or not labels_path.is_relative_to(run_dir.resolve()):
        raise ExportStaleError("shortlist labels file is missing or outside the run dir; re-run shortlist-apply")
    if sha256_file(labels_path) != labels_expected:
        raise ExportStaleError("shortlist labels changed after the shortlist was built; re-run shortlist-apply")

    selected = shortlist.get("selected")
    if not isinstance(selected, list):
        raise ExportStaleError("shortlist.json selected list is malformed; run shortlist-apply first")

    value = constraint.get("n_clips")
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ExportError("constraint.json has no valid n_clips")

    return {
        "run_dir": run_dir,
        "root": root,
        "constraint": constraint,
        "ranked": ranked,
        "manifest": manifest,
        "shortlist": shortlist,
        "shortlist_sha256": sha256_file(required["shortlist.json"]),
        "input_hashes": {
            name: sha256_file(path) for name, path in required.items()
        },
        "generation_id": generation_id,
        "n_clips": value,
    }


def _resolve_theme(constraint: dict, theme: str | None) -> str:
    text = theme if theme is not None else constraint.get("theme_text")
    return slug_theme(text if isinstance(text, str) else "")


def _check_theme_ownership(theme_dir: Path, run_id: str) -> None:
    manifest_path = theme_dir / "manifest.json"
    if not manifest_path.is_file():
        return
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ExportError(
            f"existing manifest in {theme_dir} is corrupt ({exc}); remove or repair it to re-export"
        ) from exc
    if not isinstance(existing, dict):
        raise ExportError(f"existing manifest in {theme_dir} is corrupt; remove or repair it to re-export")
    if existing.get("run_id") not in (None, run_id):
        raise ExportError(
            f"out/{theme_dir.name} is owned by another run ({existing.get('run_id')}); refusing to export"
        )


def _reject_symlink(path: Path) -> None:
    if path.is_symlink():
        raise ExportError(f"unsafe path: {path} is a symlink")


def _validated_moment(entry) -> tuple[str, int, float, float]:
    """Return the approved shortlist interval as-is.

    Export must never re-trim or continuity-adjust these timestamps: Part 3's
    gate already shaped excerpts, and Part 4's shortlist is the authorized
    selection. The encode step maps this exact [start_s, end_s) through K.
    """
    if not isinstance(entry, dict):
        raise ExportStaleError("shortlist.json selected entry is not an object; run shortlist-apply first")
    video_id = entry.get("video_id")
    index = entry.get("excerpt_index")
    start = entry.get("start_s")
    end = entry.get("end_s")
    if not isinstance(video_id, str) or not video_id:
        raise ExportStaleError("shortlist.json selected entry has no video_id")
    if isinstance(index, bool) or not isinstance(index, int):
        raise ExportStaleError("shortlist.json selected entry has no integer excerpt_index")
    if (
        isinstance(start, bool)
        or isinstance(end, bool)
        or not isinstance(start, (int, float))
        or not isinstance(end, (int, float))
    ):
        raise ExportStaleError("shortlist.json selected entry has no numeric interval")
    start, end = float(start), float(end)
    if not (math.isfinite(start) and math.isfinite(end) and end > start):
        raise ExportStaleError("shortlist.json selected entry has an invalid interval")
    return video_id, index, start, end


def _excluded_identities(shortlist: dict) -> set[tuple[str, int]]:
    identities: set[tuple[str, int]] = set()
    for entry in shortlist.get("excluded") or []:
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("video_id"), str)
            and isinstance(entry.get("excerpt_index"), int)
            and not isinstance(entry.get("excerpt_index"), bool)
        ):
            identities.add((entry["video_id"], entry["excerpt_index"]))
    return identities


def _plan_moments(inputs: dict, max_height: int) -> list[dict]:
    root: Path = inputs["root"]
    ranked_by_video: dict[str, dict] = {}
    for row in inputs["ranked"]:
        if isinstance(row, dict) and isinstance(row.get("video_id"), str):
            ranked_by_video[row["video_id"]] = row

    # Validate every selected entry before sorting (no raw exceptions), and
    # refuse duplicate identities or selected/excluded overlap.
    validated: list[tuple[str, int, float, float]] = []
    seen: set[tuple[str, int]] = set()
    excluded = _excluded_identities(inputs["shortlist"])
    for entry in inputs["shortlist"]["selected"]:
        video_id, index, start, end = _validated_moment(entry)
        identity = (video_id, index)
        if identity in seen:
            raise ExportStaleError(
                f"shortlist.json selects {video_id}:{index} more than once; re-run shortlist-apply"
            )
        if identity in excluded:
            raise ExportStaleError(
                f"shortlist.json selects {video_id}:{index} but also excludes it; re-run shortlist-apply"
            )
        seen.add(identity)
        # Honor the shortlist interval exactly — no continuity re-trim at export.
        validated.append((video_id, index, start, end))
    validated.sort(key=lambda item: (item[0], item[1]))

    metadata_cache: dict[str, tuple[dict | None, str | None]] = {}
    moments: list[dict] = []
    for video_id, index, start, end in validated:
        start_ms, end_ms = source_ms(start), source_ms(end)
        row = ranked_by_video.get(video_id)

        if video_id not in metadata_cache:
            path = _metadata_path(root, video_id)
            if not path.is_file():
                metadata_cache[video_id] = (None, "metadata_missing")
            else:
                try:
                    doc = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    metadata_cache[video_id] = (None, "metadata_invalid")
                else:
                    metadata_cache[video_id] = (doc, None) if isinstance(doc, dict) else (None, "metadata_invalid")
        metadata, meta_problem = metadata_cache[video_id]

        duration_s = None
        if row is not None and isinstance(row.get("duration_s"), (int, float)) and not isinstance(row.get("duration_s"), bool):
            duration_s = float(row["duration_s"])
        elif metadata is not None and isinstance(metadata.get("duration"), (int, float)) and not isinstance(metadata.get("duration"), bool):
            duration_s = float(metadata["duration"])

        acq_start_ms = max(0, start_ms - int(MARGIN_S * 1000))
        acq_end_ms = end_ms + int(MARGIN_S * 1000)
        if duration_s is not None:
            acq_end_ms = min(acq_end_ms, source_ms(duration_s))

        spec = None
        reason = meta_problem
        if reason is None:
            if row is None:
                reason = "recorded_dimensions_missing"
            else:
                spec, reason = resolve_export_spec(
                    metadata.get("formats") if metadata is not None else None,
                    row.get("width"),
                    row.get("height"),
                    max_height,
                )
        if reason is None and duration_s is not None and source_ms(duration_s) < end_ms:
            spec, reason = None, "coverage_impossible"
        if reason is None and acq_end_ms <= start_ms:
            spec, reason = None, "coverage_impossible"

        moments.append(
            {
                "video_id": video_id,
                "excerpt_index": index,
                "start_ms": start_ms,
                "end_ms": end_ms,
                "acq_start_ms": acq_start_ms,
                "acq_end_ms": acq_end_ms,
                "status": "ready" if spec is not None else "unplannable",
                "reason": reason,
                "spec": spec,
            }
        )
    return moments


def _plan_and_write(inputs: dict, theme: str, max_height: int) -> dict:
    plan = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "run_id": inputs["run_dir"].name,
        "theme": theme,
        "policy": EXPORT_POLICY,
        "recipe": EXPORT_RECIPE,
        "margin_s": MARGIN_S,
        "max_height": max_height,
        "shortlist_sha256": inputs["shortlist_sha256"],
        "generation_id": inputs["generation_id"],
        "n_clips": inputs["n_clips"],
        "moments": _plan_moments(inputs, max_height),
    }
    theme_dir = inputs["root"] / "out" / theme
    theme_dir.mkdir(parents=True, exist_ok=True)
    write_json_atomic(theme_dir / "plan.json", plan)
    return plan


def build_export_plan(
    run_dir: str | Path, root: str | Path, theme: str | None = None, config: dict | None = None
) -> dict:
    max_height = resolve_max_height(config)
    inputs = _load_inputs(Path(run_dir), Path(root))
    theme_final = _resolve_theme(inputs["constraint"], theme)
    theme_dir = Path(root) / "out" / theme_final
    _reject_symlink(Path(root) / "out")
    _reject_symlink(theme_dir)
    with _theme_lock(theme_dir):
        _check_theme_ownership(theme_dir, inputs["run_dir"].name)
        return _plan_and_write(inputs, theme_final, max_height)


@contextmanager
def _theme_lock(theme_dir: Path):
    theme_dir.mkdir(parents=True, exist_ok=True)
    lock_path = theme_dir / ".export.lock"
    with lock_path.open("a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _cleanup_clip_staging(clips_dir: Path) -> None:
    for candidate in clips_dir.glob("*.staging-*"):
        if candidate.is_file():
            candidate.unlink()


def _fsync_file(path: Path) -> None:
    with path.open("rb") as handle:
        os.fsync(handle.fileno())


def _fsync_dir(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


@lru_cache(maxsize=1)
def _ffmpeg_version() -> str:
    result = subprocess.run(["ffmpeg", "-version"], capture_output=True, text=True, timeout=60)
    first = (result.stdout or "").splitlines()
    return first[0].strip() if first else "unknown"


def encode_clip(
    acq_path: str | Path,
    local_start_s: float,
    local_end_s: float,
    dest: str | Path,
    *,
    ffmpeg: str = "ffmpeg",
    runner=None,
) -> None:
    """ONE controlled encode: frame-accurate trim of the decoded interior."""
    dest_path = Path(dest)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        ffmpeg,
        "-nostdin",
        "-v",
        "error",
        "-xerror",
        "-threads",
        "1",
        "-i",
        str(acq_path),
        "-map",
        "0:v:0",
        "-an",
        "-sn",
        "-dn",
        "-vf",
        f"trim=start={float(local_start_s):.6f}:end={float(local_end_s):.6f},setpts=PTS-STARTPTS",
        "-c:v",
        "libx264",
        "-preset",
        ENCODE_PRESET,
        "-crf",
        str(ENCODE_CRF),
        "-threads:v",
        "1",
        "-pix_fmt",
        "yuv420p",
        "-fps_mode",
        "passthrough",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-movflags",
        "+faststart",
        str(dest_path),
    ]
    run = runner or subprocess.run
    result = run(cmd, capture_output=True, text=True, timeout=ENCODE_TIMEOUT_S)
    if result.returncode != 0:
        raise RuntimeError(
            (result.stderr or result.stdout or f"encode failed with exit {result.returncode}").strip()
        )


def _decode_clean(path: Path) -> None:
    try:
        decode = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-map", "0:v:0", "-f", "null", "-"],
            capture_output=True,
            text=True,
            timeout=900,
        )
    except subprocess.TimeoutExpired as exc:
        raise ExportMediaError("probe_timeout", f"decode of {path.name} timed out") from exc
    if decode.returncode != 0:
        raise ExportMediaError(
            "decode_failed", (decode.stderr or decode.stdout or "decode failed").strip()
        )


def _validate_final_clip(
    path: Path, spec: dict, local_start_s: float, local_end_s: float, max_height: int
) -> dict:
    info = probe_export_coverage(path)
    problem = clip_media_problem(
        info,
        local_start_s=local_start_s,
        local_end_s=local_end_s,
        width=int(spec["width"]),
        height=int(spec["height"]),
        max_height=max_height,
    )
    if problem is not None:
        raise ExportMediaError(*problem)
    _decode_clean(path)
    return info


def _previous_entries(theme_dir: Path, run_id: str, pointer: dict | None) -> tuple[dict, str | None]:
    """Prior clips for reuse, trusted only through a validated publication."""
    manifest_path = theme_dir / "manifest.json"
    if not manifest_path.is_file():
        return {}, None
    if pointer is not None:
        if pointer.get("run_id") != run_id or pointer.get("theme") != theme_dir.name:
            return {}, None
        try:
            expected = pointer.get("manifest_sha256")
            if not isinstance(expected, str) or sha256_file(manifest_path) != expected:
                return {}, None
        except OSError:
            return {}, None
    try:
        doc = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}, None
    if not isinstance(doc, dict) or doc.get("run_id") != run_id:
        return {}, None
    if doc.get("schema_version") != EXPORT_SCHEMA_VERSION:
        return {}, None
    entries: dict[tuple[str, int], dict] = {}
    for entry in doc.get("clips") or []:
        if not isinstance(entry, dict):
            continue
        video_id = entry.get("video_id")
        index = entry.get("excerpt_index")
        if not isinstance(video_id, str) or isinstance(index, bool) or not isinstance(index, int):
            continue
        key = (video_id, index)
        if key in entries:
            continue
        file_field = entry.get("file")
        if not isinstance(file_field, str) or not file_field.startswith("clips/"):
            continue
        if "/" in file_field[len("clips/"):] or ".." in file_field:
            continue
        entries[key] = entry
    toolchain = doc.get("toolchain") or {}
    return entries, toolchain.get("ffmpeg")


def _reusable_entry(
    previous: dict,
    key: tuple[str, int],
    *,
    start_ms: int,
    end_ms: int,
    acq_span_ms: list[int],
    mapping_k_ms: int,
    spec: dict,
    acq_sha256: str,
    clips_dir: Path,
    ffmpeg_version: str,
    previous_ffmpeg,
    max_height: int,
) -> dict | None:
    entry = previous.get(key)
    if not entry:
        return None
    canonical = clip_name(key[0], key[1], start_ms, end_ms)
    if (
        entry.get("start_ms") != start_ms
        or entry.get("end_ms") != end_ms
        or entry.get("acq_span_ms") != acq_span_ms
        or entry.get("mapping_k_ms") != mapping_k_ms
        or str(entry.get("format_id")) != str(spec["format_id"])
        or entry.get("acq_sha256") != acq_sha256
        or entry.get("recipe") != EXPORT_RECIPE
        or previous_ffmpeg != ffmpeg_version
        or entry.get("file") != f"clips/{canonical}"
    ):
        return None
    clip_path = clips_dir / canonical
    if clip_path.is_symlink() or not clip_path.is_file():
        return None
    if clip_path.stat().st_size != entry.get("size_bytes"):
        return None
    if sha256_file(clip_path) != entry.get("sha256"):
        return None
    try:
        info = probe_export_coverage(clip_path)
    except Exception:
        return None
    problem = clip_media_problem(
        info,
        local_start_s=(start_ms - mapping_k_ms) / 1000.0,
        local_end_s=(end_ms - mapping_k_ms) / 1000.0,
        width=int(spec["width"]),
        height=int(spec["height"]),
        max_height=max_height,
    )
    if problem is not None or info["n_frames"] != entry.get("n_frames"):
        return None
    return dict(entry)


def _read_pointer(run_dir: Path) -> dict | None:
    pointer_path = run_dir / "export.json"
    if not pointer_path.is_file():
        return None
    try:
        doc = json.loads(pointer_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return doc if isinstance(doc, dict) else None


def export_run(
    run_dir: str | Path,
    root: str | Path,
    theme: str | None = None,
    allow_export: bool = False,
    yt=None,
    config: dict | None = None,
) -> dict:
    run_dir = Path(run_dir)
    root = Path(root)
    if not allow_export and not (isinstance(config, dict) and config.get("allow_export") is True):
        raise ExportError(
            "export requires explicit authorization: pass --allow-export (or set allow_export: true in config)"
        )
    max_height = resolve_max_height(config)
    inputs = _load_inputs(run_dir, root)
    theme_final = _resolve_theme(inputs["constraint"], theme)
    out_root = root / "out"
    theme_dir = out_root / theme_final
    clips_dir = theme_dir / "clips"
    export_cache_dir = root / "data" / "cache" / "export"
    _reject_symlink(out_root)
    _reject_symlink(theme_dir)
    _reject_symlink(clips_dir)

    free = shutil.disk_usage(root).free
    if free < MIN_FREE_DISK_BYTES:
        raise ExportError(
            f"insufficient free disk under {root}: {free} bytes available, need {MIN_FREE_DISK_BYTES}"
        )

    with _theme_lock(theme_dir):
        _check_theme_ownership(theme_dir, run_dir.name)
        plan = _plan_and_write(inputs, theme_final, max_height)
        plan_sha256 = sha256_file(theme_dir / "plan.json")
        clips_dir.mkdir(parents=True, exist_ok=True)
        _cleanup_clip_staging(clips_dir)

        previous, previous_ffmpeg = _previous_entries(theme_dir, run_dir.name, _read_pointer(run_dir))
        yt_client = yt if yt is not None else YtDlp(
            tmp_dir=root / "tmp",
            allow_download=False,
            timeout=60,
            allow_export=True,
            export_cache_dir=export_cache_dir,
        )
        ffmpeg_version = _ffmpeg_version()

        clips: list[dict] = []
        failed: list[dict] = []

        def record_failure(moment: dict, reason: str, detail: str, attempts: list[str] | None = None) -> None:
            entry = {
                "video_id": moment["video_id"],
                "excerpt_index": moment["excerpt_index"],
                "reason": reason,
                "detail": detail[:300],
            }
            if attempts and len(attempts) > 1:
                entry["attempts"] = len(attempts)
                entry["attempt_errors"] = [item[:200] for item in attempts]
            failed.append(entry)

        for moment in plan["moments"]:
            key = (moment["video_id"], moment["excerpt_index"])
            if moment["status"] != "ready" or not isinstance(moment.get("spec"), dict):
                record_failure(moment, moment.get("reason") or "unplannable", "format resolution failed during planning")
                continue

            spec = moment["spec"]
            span = (moment["acq_start_ms"] / 1000.0, moment["acq_end_ms"] / 1000.0)
            export_spec = ExportSpec(
                video_id=moment["video_id"],
                format_id=spec["format_id"],
                width=spec["width"],
                height=spec["height"],
                codec=spec["codec"],
                span=span,
                clip_start_ms=moment["start_ms"],
                clip_end_ms=moment["end_ms"],
            )

            acq_path: Path | None = None
            attempt_errors: list[str] = []
            failure_code: str | None = None
            for _attempt in range(EXPORT_ATTEMPTS):
                try:
                    acq_path = Path(yt_client.fetch_export(export_spec))
                    break
                except Exception as exc:  # noqa: BLE001 - recorded with a reason code
                    attempt_errors.append(f"{type(exc).__name__}: {exc}")
                    if isinstance(exc, ExportMediaError):
                        failure_code = exc.code
            if acq_path is None:
                record_failure(moment, failure_code or "download_failed", attempt_errors[-1] if attempt_errors else "unknown", attempt_errors)
                continue

            try:
                acq_sha256 = sha256_file(acq_path)
                info = probe_export_coverage(acq_path)
            except ExportMediaError as exc:
                record_failure(moment, exc.code, str(exc))
                continue
            except subprocess.TimeoutExpired as exc:
                record_failure(moment, "probe_timeout", str(exc))
                continue
            except Exception as exc:  # noqa: BLE001
                record_failure(moment, "probe_failed", str(exc))
                continue

            k_ms = source_ms(info["first_pts_s"])
            last_end_ms = source_ms(info["last_end_s"])
            problem = coverage_problem(moment["start_ms"], moment["end_ms"], k_ms, last_end_ms)
            if problem is not None:
                record_failure(moment, "coverage_missing", problem)
                continue
            if (info["width"], info["height"]) != (int(spec["width"]), int(spec["height"])):
                record_failure(
                    moment, "dimension_mismatch", f"acquisition is {info['width']}x{info['height']}"
                )
                continue

            acq_span_ms = [moment["acq_start_ms"], moment["acq_end_ms"]]
            prior = _reusable_entry(
                previous,
                key,
                start_ms=moment["start_ms"],
                end_ms=moment["end_ms"],
                acq_span_ms=acq_span_ms,
                mapping_k_ms=k_ms,
                spec=spec,
                acq_sha256=acq_sha256,
                clips_dir=clips_dir,
                ffmpeg_version=ffmpeg_version,
                previous_ffmpeg=previous_ffmpeg,
                max_height=max_height,
            )
            if prior is not None:
                clips.append(prior)
                continue

            name = clip_name(moment["video_id"], moment["excerpt_index"], moment["start_ms"], moment["end_ms"])
            final_path = clips_dir / name
            staged = clips_dir / f"{name[:-4]}.staging-{uuid.uuid4().hex}.mp4"
            local_start = (moment["start_ms"] - k_ms) / 1000.0
            local_end = (moment["end_ms"] - k_ms) / 1000.0
            clip_info = None
            try:
                encode_clip(acq_path, local_start, local_end, staged)
                clip_info = _validate_final_clip(staged, spec, local_start, local_end, max_height)
            except ExportMediaError as exc:
                record_failure(moment, exc.code, str(exc))
            except subprocess.TimeoutExpired as exc:
                record_failure(moment, "encode_timeout", str(exc))
            except Exception as exc:  # noqa: BLE001
                record_failure(moment, "encode_failed", str(exc))
            if clip_info is None:
                staged.unlink(missing_ok=True)
                continue

            os.replace(staged, final_path)
            _fsync_file(final_path)
            duration_ms = round(clip_info["last_end_s"] * 1000)
            clips.append(
                {
                    "video_id": moment["video_id"],
                    "excerpt_index": moment["excerpt_index"],
                    "start_ms": moment["start_ms"],
                    "end_ms": moment["end_ms"],
                    "acq_span_ms": acq_span_ms,
                    "mapping_k_ms": k_ms,
                    "format_id": str(spec["format_id"]),
                    "codec": spec["codec"],
                    "width": clip_info["width"],
                    "height": clip_info["height"],
                    "file": f"clips/{name}",
                    "size_bytes": final_path.stat().st_size,
                    "sha256": sha256_file(final_path),
                    "duration_ms": duration_ms,
                    "n_frames": clip_info["n_frames"],
                    "acq_sha256": acq_sha256,
                    "recipe": EXPORT_RECIPE,
                }
            )

        _cleanup_clip_staging(clips_dir)
        clips.sort(key=lambda entry: (entry["video_id"], entry["excerpt_index"]))
        failed.sort(key=lambda entry: (entry["video_id"], entry["excerpt_index"]))
        manifest = {
            "schema_version": EXPORT_SCHEMA_VERSION,
            "run_id": run_dir.name,
            "theme": theme_final,
            "policy": EXPORT_POLICY,
            "recipe": EXPORT_RECIPE,
            "margin_s": MARGIN_S,
            "max_height": max_height,
            "toolchain": {"ffmpeg": ffmpeg_version},
            "shortlist_sha256": inputs["shortlist_sha256"],
            "generation_id": inputs["generation_id"],
            "plan_sha256": plan_sha256,
            "constraint": {
                "min_width": inputs["constraint"].get("min_width"),
                "min_height": inputs["constraint"].get("min_height"),
                "aspect_min": inputs["constraint"].get("aspect_min"),
                "aspect_max": inputs["constraint"].get("aspect_max"),
            },
            "clips": clips,
            "failed": failed,
            "counts": {
                "requested": inputs["n_clips"],
                "selected": len(plan["moments"]),
                "exported": len(clips),
                "failed": len(failed),
                "export_complete": not failed,
                "request_fulfilled": len(clips) >= inputs["n_clips"],
            },
        }

        # Refuse to publish if the run inputs changed while we were working.
        for name, path in (
            ("shortlist.json", run_dir / "shortlist.json"),
            ("constraint.json", run_dir / "constraint.json"),
            ("ranked.json", run_dir / "ranked.json"),
            ("analysis_manifest.json", run_dir / "analysis_manifest.json"),
        ):
            try:
                current = sha256_file(path)
            except OSError as exc:
                raise ExportStaleError(
                    f"{name} disappeared during export; re-run export"
                ) from exc
            if current != inputs["input_hashes"][name]:
                raise ExportStaleError(
                    f"{name} changed during export; re-run export to publish a consistent generation"
                )

        write_json_atomic(theme_dir / "manifest.json", manifest)
        _fsync_file(theme_dir / "manifest.json")
        expected_names = {entry["file"].split("/")[-1] for entry in clips}
        for candidate in clips_dir.glob("*.mp4"):
            if candidate.name not in expected_names:
                candidate.unlink()
        _fsync_dir(clips_dir)
        _fsync_dir(theme_dir)

        pointer = {
            "schema_version": POINTER_SCHEMA_VERSION,
            "run_id": run_dir.name,
            "theme": theme_final,
            "manifest_path": f"out/{theme_final}/manifest.json",
            "manifest_sha256": sha256_file(theme_dir / "manifest.json"),
            "policy": EXPORT_POLICY,
        }
        with exclusive_run_lock(run_dir):
            write_json_atomic(run_dir / "export.json", pointer)
        return manifest
