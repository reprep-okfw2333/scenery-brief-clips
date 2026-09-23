from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import uuid
from pathlib import Path

from PIL import Image

from scenery_brief_clips.analysis_cache import safe_video_id
from scenery_brief_clips.run_lock import exclusive_run_lock
from scenery_brief_clips.continuity import (
    DEFAULT_REVIEW_CONTINUITY_SUSPECT_COLOR,
    DEFAULT_REVIEW_CONTINUITY_SUSPECT_HAMMING,
    features_from_path,
    mean_rgb_distance,
)
from scenery_brief_clips.shortlist import (
    DEFAULT_FRAMES_PER_MOMENT,
    MIN_FRAMES_PER_MOMENT,
    REVIEW_SCHEMA_VERSION,
    ShortlistInputError,
    ShortlistStaleError,
    hamming64,
    parse_run_json,
    read_run_bytes,
    signature_for_frames,
)

FRAME_QUALITY = "4"


# Nudge endpoints slightly inside so ffmpeg seeks never land past EOF.
_SAMPLE_EDGE_INSET_S = 0.05


def sample_times(start_s: float, end_s: float, frames: int) -> list[float]:
    """Evenly sample a moment, always including near-start and near-end.

    Earlier revisions used (i+1)/(n+1) interior-only points, which left the
    last ~length/(n+1) seconds blind — soft dissolves past the final sample
    reached export. Endpoints are inset by min(50ms, 25% of length) so seeks
    stay inside the copy.
    """
    frames = int(frames)
    if frames < MIN_FRAMES_PER_MOMENT:
        raise ShortlistInputError(f"frames per moment must be at least {MIN_FRAMES_PER_MOMENT}")
    start_s = float(start_s)
    end_s = float(end_s)
    length = end_s - start_s
    if length <= 0:
        raise ShortlistInputError("moment has an invalid interval")
    inset = min(_SAMPLE_EDGE_INSET_S, length / 4.0)
    first = start_s + inset
    last = end_s - inset
    if frames == 2:
        return [first, last]
    interior = frames - 2
    span = last - first
    times = [first]
    for index in range(interior):
        times.append(first + (index + 1) * span / (interior + 1))
    times.append(last)
    return times


def _extract_frame(cache_path: Path, t_s: float, out_path: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{t_s:.6f}",
            "-i",
            str(cache_path),
            "-frames:v",
            "1",
            "-q:v",
            FRAME_QUALITY,
            "-y",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg frame extraction failed").strip())
    if not out_path.is_file() or out_path.stat().st_size <= 0:
        raise RuntimeError(f"no frame extracted at {t_s:.3f}s")


def _build_strip(frame_paths: list[Path], out_path: Path) -> None:
    images = [Image.open(path).convert("RGB") for path in frame_paths]
    try:
        width = sum(image.width for image in images)
        height = max(image.height for image in images)
        strip = Image.new("RGB", (width, height), (0, 0, 0))
        cursor = 0
        for image in images:
            strip.paste(image, (cursor, 0))
            cursor += image.width
        strip.save(out_path, "JPEG", quality=88)
    finally:
        for image in images:
            image.close()


def review_run(
    run_dir: str | Path,
    frames_per_moment: int = DEFAULT_FRAMES_PER_MOMENT,
) -> dict:
    run_dir = Path(run_dir)
    with exclusive_run_lock(run_dir):
        return _review_run_locked(run_dir, frames_per_moment)


def _review_run_locked(run_dir: Path, frames_per_moment: int) -> dict:
    frames_per_moment = int(frames_per_moment)
    if frames_per_moment < MIN_FRAMES_PER_MOMENT:
        raise ShortlistInputError(f"frames per moment must be at least {MIN_FRAMES_PER_MOMENT}")

    excerpts_path = run_dir / "excerpts.json"
    manifest_path = run_dir / "analysis_manifest.json"
    if not excerpts_path.is_file() or not manifest_path.is_file():
        raise ShortlistStaleError("missing excerpts.json or analysis_manifest.json; run analyze first")
    manifest = parse_run_json(
        read_run_bytes(manifest_path, "analysis_manifest.json"), "analysis_manifest.json"
    )
    if not isinstance(manifest, dict):
        raise ShortlistStaleError("analysis_manifest.json is not a JSON object; the run data is broken")
    excerpts_bytes = read_run_bytes(excerpts_path, "excerpts.json")
    excerpts_sha256 = hashlib.sha256(excerpts_bytes).hexdigest()
    if manifest.get("excerpts_sha256") != excerpts_sha256:
        raise ShortlistStaleError("excerpts.json changed after analysis; re-run analyze")

    rows = parse_run_json(excerpts_bytes, "excerpts.json")
    if not isinstance(rows, list):
        raise ShortlistStaleError("excerpts.json is not a list of rows; re-run analyze")
    review_root = run_dir / "review"
    moments: list[dict] = []
    errors: list[dict] = []
    total_frames = 0

    for row in rows:
        if not isinstance(row, dict):
            raise ShortlistStaleError("excerpts.json contains a non-object row; run analyze again")
        video_id = str(row.get("video_id") or "")
        copies = {
            str(copy.get("cache_key") or ""): copy
            for copy in row.get("copies") or []
            if isinstance(copy, dict)
        }
        excerpts_field = row.get("excerpts") or []
        if not isinstance(excerpts_field, list):
            raise ShortlistStaleError(
                "excerpts.json contains a non-list excerpts field; run analyze again"
            )
        for index, excerpt in enumerate(excerpts_field):
            if not isinstance(excerpt, dict):
                raise ShortlistStaleError(
                    "excerpts.json contains a non-object excerpt; run analyze again"
                )
            stage = "extract"
            try:
                cache_key = str(excerpt.get("analysis_cache_key") or "")
                copy = copies.get(cache_key)
                if copy is None:
                    raise RuntimeError(f"excerpt references unknown cache key {cache_key}")
                cache_path = Path(str(copy.get("path") or ""))
                if not cache_path.is_file():
                    raise RuntimeError(f"missing analysis media {cache_path}")

                moment_dir = review_root / safe_video_id(video_id) / str(index)
                if not moment_dir.resolve().is_relative_to(review_root.resolve()):
                    raise RuntimeError(
                        f"refusing to write outside the review directory for {video_id!r}"
                    )
                if moment_dir.is_dir():
                    shutil.rmtree(moment_dir)
                moment_dir.mkdir(parents=True)

                start_s = float(excerpt["start_s"])
                end_s = float(excerpt["end_s"])
                span = excerpt.get("analysis_span") or copy.get("span")
                if not isinstance(span, (list, tuple)) or len(span) != 2:
                    raise RuntimeError("excerpt has no analysis_span to map times into the copy")
                offset = float(span[0])
                frame_records = []
                frame_paths = []
                seen_frame_names: set[str] = set()
                for t_s in sample_times(start_s, end_s, frames_per_moment):
                    local_t = t_s - offset
                    if local_t < 0:
                        raise RuntimeError(
                            f"sample time {t_s:.3f}s precedes the analysis copy span"
                        )
                    frame_name = f"frame_{round(t_s * 1_000_000)}.jpg"
                    if frame_name in seen_frame_names:
                        raise RuntimeError(f"duplicate frame sample name {frame_name}")
                    seen_frame_names.add(frame_name)
                    frame_path = moment_dir / frame_name
                    _extract_frame(cache_path, local_t, frame_path)
                    frame_paths.append(frame_path)
                    frame_records.append(
                        {
                            "t_s": round(t_s, 3),
                            "path": frame_path.relative_to(run_dir).as_posix(),
                        }
                    )
                stage = "build"
                strip_path = moment_dir / "strip.jpg"
                _build_strip(frame_paths, strip_path)
                total_frames += len(frame_paths)
                frame_hashes = signature_for_frames(frame_paths)
                first_feat = features_from_path(frame_paths[0])
                last_feat = features_from_path(frame_paths[-1])
                endpoint_hamming = hamming64(first_feat.dhash, last_feat.dhash)
                endpoint_color = mean_rgb_distance(first_feat.mean_rgb, last_feat.mean_rgb)
                moment = {
                    "video_id": video_id,
                    "excerpt_index": index,
                    "start_s": start_s,
                    "end_s": end_s,
                    "analysis_cache_key": cache_key,
                    "frames": frame_records,
                    "frame_hashes": frame_hashes,
                    "strip": strip_path.relative_to(run_dir).as_posix(),
                    "continuity_endpoint_hamming": endpoint_hamming,
                    "continuity_endpoint_color": round(endpoint_color, 3),
                }
                if (
                    endpoint_hamming > DEFAULT_REVIEW_CONTINUITY_SUSPECT_HAMMING
                    or endpoint_color > DEFAULT_REVIEW_CONTINUITY_SUSPECT_COLOR
                ):
                    moment["continuity_suspect"] = True
                moments.append(moment)
            except Exception as exc:
                errors.append(
                    {
                        "video_id": video_id,
                        "excerpt_index": index,
                        "stage": stage,
                        "error": str(exc),
                    }
                )

    doc = {
        "schema_version": REVIEW_SCHEMA_VERSION,
        "excerpts_sha256": excerpts_sha256,
        "generation_id": manifest.get("generation_id"),
        "frames_per_moment": frames_per_moment,
        "moments": moments,
        "errors": errors,
        "counts": {
            "moments": len(moments),
            "frames": total_frames,
            "errors": len(errors),
        },
    }

    token = uuid.uuid4().hex
    tmp = run_dir / f".review.{token}.tmp"
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            json.dump(doc, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, run_dir / "review.json")
    finally:
        tmp.unlink(missing_ok=True)
    return doc
