from __future__ import annotations

import json
import math
import subprocess
from collections import Counter
from collections.abc import Callable
from pathlib import Path

from scenery_brief_clips.analysis_cache import (
    ANALYSIS_CACHE_POLICY,
    ANALYSIS_MARKER_SCHEMA_VERSION,
    EXPORT_CACHE_POLICY,
    EXPORT_CAP_MAX,
    EXPORT_CAP_MIN,
    analysis_cache_path,
    analysis_marker_path,
    canonical_span_ms,
    canonical_span_seconds,
    export_cache_path,
    sha256_file,
)
from scenery_brief_clips.analyze import analysis_plan
from scenery_brief_clips.pipeline_analyze import ANALYSIS_SCHEMA_VERSION, KEEP_PRIORITIES, analysis_generation_id
from scenery_brief_clips.analysis_cache import MEDIA_DURATION_TOLERANCE_S
from scenery_brief_clips.run_lock import exclusive_run_lock
from scenery_brief_clips.shortlist import SHORTLIST_SCHEMA_VERSION, build_shortlist
from scenery_brief_clips.export import (
    EXPORT_POLICY,
    EXPORT_RECIPE,
    EXPORT_SCHEMA_VERSION as EXPORT_DOC_SCHEMA_VERSION,
    POINTER_SCHEMA_VERSION,
    CLIP_DURATION_TOLERANCE_MS,
    clip_media_problem,
    expected_clip_frames,
    source_ms,
)
from scenery_brief_clips.yt import export_marker_path, probe_export_coverage

ProbeFn = Callable[[Path], dict]
DecodeFn = Callable[[Path], None]
ExportProbeFn = Callable[[Path], dict]


def _int(value, default: int = 0) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


REQUIRED_SETTINGS = {
    "max_videos",
    "max_analysis_s",
    "pad_s",
    "min_scene_len_s",
    "target_duration_s",
    "duration_min_s",
    "duration_max_s",
    "acquisition_attempts",
    "cache_policy",
}


def _default_probe(path: Path) -> dict:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_streams",
            "-show_format",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffprobe failed").strip())
    payload = json.loads(result.stdout)
    streams = payload.get("streams") or []
    videos = [stream for stream in streams if stream.get("codec_type") == "video"]
    audios = [stream for stream in streams if stream.get("codec_type") == "audio"]
    width = max((int(stream.get("width") or 0) for stream in videos), default=0)
    height = max((int(stream.get("height") or 0) for stream in videos), default=0)
    duration_raw = (payload.get("format") or {}).get("duration")
    duration_s = float(duration_raw) if duration_raw not in (None, "N/A") else None
    return {
        "video_streams": len(videos),
        "audio_streams": len(audios),
        "width": width,
        "height": height,
        "duration_s": duration_s,
    }


def _default_decode(path: Path) -> None:
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-xerror",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        timeout=300,
    )
    if result.returncode != 0:
        raise RuntimeError((result.stderr or result.stdout or "ffmpeg decode failed").strip())


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _finite_number(value) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _finite_pair(value) -> tuple[float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    start = _finite_number(value[0])
    end = _finite_number(value[1])
    if start is None or end is None or end <= start:
        return None
    return start, end


def _span_key(value) -> tuple[int, int] | None:
    pair = _finite_pair(value)
    if pair is None:
        return None
    try:
        return canonical_span_ms(pair)
    except ValueError:
        return None


def _base_report(run_dir: Path, errors: list[str], warnings: list[str]) -> dict:
    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "run_dir": str(run_dir),
    }


def _validate_settings(manifest: dict, errors: list[str]) -> dict | None:
    settings = manifest.get("settings")
    if not isinstance(settings, dict):
        errors.append("analysis manifest settings must be an object")
        return None
    for key in sorted(REQUIRED_SETTINGS):
        if key not in settings:
            errors.append(f"missing manifest setting {key}")
    if any(key not in settings for key in REQUIRED_SETTINGS):
        return settings
    if settings.get("cache_policy") != ANALYSIS_CACHE_POLICY:
        errors.append(
            f"manifest setting cache_policy is {settings.get('cache_policy')!r}, expected {ANALYSIS_CACHE_POLICY!r}"
        )
    try:
        max_videos = int(settings["max_videos"])
        acquisition_attempts = int(settings["acquisition_attempts"])
    except (TypeError, ValueError):
        errors.append("manifest integer settings are invalid")
    else:
        if max_videos < 1:
            errors.append("manifest setting max_videos must be at least 1")
        if acquisition_attempts < 1:
            errors.append("manifest setting acquisition_attempts must be at least 1")
    for key in (
        "max_analysis_s",
        "min_scene_len_s",
        "target_duration_s",
        "duration_min_s",
        "duration_max_s",
    ):
        value = _finite_number(settings.get(key))
        if value is None or value <= 0:
            errors.append(f"manifest setting {key} must be finite and greater than 0")
    pad_s = _finite_number(settings.get("pad_s"))
    if pad_s is None or pad_s < 0:
        errors.append("manifest setting pad_s must be finite and at least 0")
    return settings


def _expected_rows(ranked: list[dict], settings: dict, errors: list[str]) -> dict[str, dict]:
    expected: dict[str, dict] = {}
    analyzed = 0
    try:
        max_videos = int(settings["max_videos"])
        max_analysis_s = float(settings["max_analysis_s"])
        pad_s = float(settings["pad_s"])
    except (KeyError, TypeError, ValueError):
        return expected

    for item in ranked:
        if not isinstance(item, dict):
            errors.append("ranked.json contains a non-object row")
            continue
        video_id = str(item.get("video_id") or "")
        if not video_id:
            errors.append("ranked.json contains a row without video_id")
            continue
        if video_id in expected:
            errors.append(f"duplicate ranked row {video_id}")
            continue
        priority = str(item.get("priority") or "")
        if priority not in KEEP_PRIORITIES:
            expected[video_id] = {
                "kind": "skipped",
                "reason": priority or "ineligible",
                "priority": priority,
            }
            continue
        if analyzed >= max_videos:
            expected[video_id] = {
                "kind": "skipped",
                "reason": "max_videos",
                "priority": priority,
            }
            continue
        analyzed += 1
        try:
            plan = analysis_plan(
                duration_s=float(item.get("duration_s") or 0.0),
                windows=item.get("windows") or [],
                max_analysis_s=max_analysis_s,
                pad_s=pad_s,
            )
            ranges = [canonical_span_seconds(span) for span in plan.ranges]
        except Exception as exc:
            errors.append(f"cannot recompute analysis plan for {video_id}: {exc}")
            ranges = []
            plan_mode = "invalid"
        else:
            plan_mode = plan.mode
        expected[video_id] = {
            "kind": "analyzed",
            "priority": priority,
            "plan_mode": plan_mode,
            "ranges": ranges,
        }
    return expected


def _validate_marker(
    path: Path,
    video_id: str,
    span: tuple[float, float],
    expected_size: int,
    expected_hash: str,
    errors: list[str],
) -> None:
    marker_path = analysis_marker_path(path)
    if not marker_path.is_file():
        errors.append(f"missing completion marker for {path}")
        return
    try:
        marker = _load_json(marker_path)
    except Exception as exc:
        errors.append(f"cannot load completion marker for {path}: {exc}")
        return
    if not isinstance(marker, dict):
        errors.append(f"completion marker is not an object for {path}")
        return
    expected_span_ms = list(canonical_span_ms(span))
    checks = {
        "schema_version": ANALYSIS_MARKER_SCHEMA_VERSION,
        "cache_policy": ANALYSIS_CACHE_POLICY,
        "video_id": video_id,
        "span_ms": expected_span_ms,
        "size_bytes": expected_size,
        "sha256": expected_hash,
    }
    for key, expected_value in checks.items():
        if marker.get(key) != expected_value:
            errors.append(f"completion marker {key} mismatch for {path}")


def _duplicate_reference_error(
    entry_key: tuple[str, int],
    reference: str,
    candidate_keys: set[tuple[str, int]],
    excluded_by_key: dict[tuple[str, int], list],
) -> str | None:
    """A duplicate reference must name a real candidate that is not itself a dedup loser.

    The referenced moment may be excluded (for example 'beyond n_clips' when the
    quota is small): that is the dedup winner losing the quota race, and it is
    allowed. References to unknown moments, to the entry itself, or to another
    duplicate are rejected.
    """
    match = next(
        (candidate for candidate in candidate_keys if f"{candidate[0]}:{candidate[1]}" == reference),
        None,
    )
    if match is None:
        return f"duplicate reference {reference!r} does not match any analyzed moment"
    if match == entry_key:
        return f"excluded moment {entry_key[0]}:{entry_key[1]} claims to duplicate itself"
    if any(str(other).startswith("duplicate of ") for other in excluded_by_key.get(match, [])):
        return f"duplicate reference {reference!r} points at another duplicate"
    return None


def _check_shortlist(
    run_dir: Path,
    excerpts_rows: list,
    constraint: dict,
    manifest: dict,
    errors: list[str],
) -> dict | None:
    shortlist_path = run_dir / "shortlist.json"
    if not shortlist_path.is_file():
        return None
    try:
        doc = _load_json(shortlist_path)
    except Exception as exc:
        errors.append(f"cannot load shortlist.json: {exc}")
        return {"present": True}
    if not isinstance(doc, dict):
        errors.append("shortlist.json must contain an object")
        return {"present": True}
    if doc.get("schema_version") != SHORTLIST_SCHEMA_VERSION:
        errors.append(f"unsupported shortlist schema {doc.get('schema_version')}")
    bindings = doc.get("bindings")
    if not isinstance(bindings, dict):
        errors.append("shortlist bindings are missing")
        bindings = {}
    if bindings.get("excerpts_sha256") != sha256_file(run_dir / "excerpts.json"):
        errors.append("shortlist is stale: excerpts.json changed after shortlist was applied; re-run shortlist-apply")
    if bindings.get("generation_id") != manifest.get("generation_id"):
        errors.append("shortlist generation does not match the analysis manifest; re-run shortlist-apply")

    labels_ok = False
    recorded_labels = str(bindings.get("labels_path") or "")
    labels_path: Path | None = None
    if not recorded_labels:
        errors.append("shortlist bindings do not record the labels file")
    else:
        labels_path = Path(recorded_labels)
        if not labels_path.is_absolute():
            labels_path = run_dir / labels_path
        labels_path = labels_path.resolve()
        if not labels_path.is_relative_to(run_dir.resolve()):
            errors.append(f"shortlist labels file must stay inside the run dir: {labels_path}")
        elif not labels_path.is_file():
            errors.append(f"shortlist labels file is missing: {labels_path}")
        elif sha256_file(labels_path) != bindings.get("labels_sha256"):
            errors.append(
                "shortlist labels file changed after shortlist was applied; re-run shortlist-apply"
            )
        else:
            labels_ok = True

    review_path = run_dir / "review.json"
    review_ok = False
    if not review_path.is_file():
        errors.append("review.json is missing but a shortlist is present")
    elif sha256_file(review_path) != bindings.get("review_sha256"):
        errors.append("review material changed after shortlist was applied; re-run shortlist-apply")
    else:
        review_ok = True
    if review_ok:
        try:
            review_doc = _load_json(review_path)
        except Exception as exc:
            errors.append(f"cannot load review.json: {exc}")
            review_ok = False
        else:
            if not isinstance(review_doc, dict) or review_doc.get(
                "excerpts_sha256"
            ) != manifest.get("excerpts_sha256"):
                errors.append(
                    "review.json does not match the analysis generation; re-run shortlist-review"
                )
                review_ok = False

    selected = doc.get("selected")
    excluded = doc.get("excluded")
    if not isinstance(selected, list) or not isinstance(excluded, list):
        errors.append("shortlist selected/excluded must be arrays")
        return {"present": True}
    counts = doc.get("counts")
    if not isinstance(counts, dict):
        errors.append("shortlist counts are missing")
        counts = {}
    shortfall = doc.get("shortfall")
    if not isinstance(shortfall, dict):
        errors.append("shortfall block is missing")
        shortfall = {}

    try:
        n_clips_requested = int(doc.get("n_clips_requested"))
    except (TypeError, ValueError):
        errors.append("shortlist n_clips_requested is invalid")
        n_clips_requested = -1
    try:
        constraint_n_clips = int(constraint.get("n_clips"))
    except (TypeError, ValueError):
        constraint_n_clips = None
    if n_clips_requested >= 0 and constraint_n_clips != n_clips_requested:
        errors.append("shortlist n_clips does not match constraint")

    if counts.get("n_selected") != len(selected):
        errors.append("shortlist counts do not reconcile (n_selected)")
    if counts.get("n_excluded") != len(excluded):
        errors.append("shortlist counts do not reconcile (n_excluded)")
    if counts.get("n_candidates") != len(selected) + len(excluded):
        errors.append("shortlist counts do not reconcile (selected+excluded != candidates)")
    if n_clips_requested >= 0:
        if len(selected) > n_clips_requested:
            errors.append("shortlist selects more moments than n_clips allows")
        expected_shortfall = max(0, n_clips_requested - len(selected))
        if shortfall.get("count") != expected_shortfall:
            errors.append("shortfall count does not reconcile")
        if expected_shortfall > 0 and not str(shortfall.get("explanation") or "").strip():
            errors.append("shortfall explanation is missing")
    for entry in excluded:
        if not isinstance(entry, dict) or not entry.get("reasons"):
            errors.append("excluded moment is missing reasons")
            break

    copies_by_video: dict[str, set[str]] = {}
    for row in excerpts_rows:
        if not isinstance(row, dict):
            continue
        video_id = str(row.get("video_id") or "")
        keys = {
            str(copy.get("cache_key") or "")
            for copy in row.get("copies") or []
            if isinstance(copy, dict)
        }
        copies_by_video[video_id] = keys
    selected_keys: set[tuple[str, int]] = set()
    for entry in selected:
        if not isinstance(entry, dict):
            errors.append("selected moment is not an object")
            continue
        video_id = str(entry.get("video_id") or "")
        try:
            index = int(entry.get("excerpt_index"))
        except (TypeError, ValueError):
            errors.append("selected moment has no valid excerpt_index")
            continue
        selected_keys.add((video_id, index))
        if str(entry.get("analysis_cache_key") or "") not in copies_by_video.get(video_id, set()):
            errors.append(f"selected moment {video_id}:{index} references unknown analysis media")
    candidate_keys: set[tuple[str, int]] = set()
    for row in excerpts_rows:
        if not isinstance(row, dict):
            continue
        video_id = str(row.get("video_id") or "")
        for index in range(len(row.get("excerpts") or [])):
            candidate_keys.add((video_id, index))
    excluded_by_key: dict[tuple[str, int], list] = {}
    for entry in excluded:
        if not isinstance(entry, dict):
            continue
        try:
            key = (str(entry.get("video_id") or ""), int(entry.get("excerpt_index")))
        except (TypeError, ValueError):
            continue
        excluded_by_key[key] = list(entry.get("reasons") or [])
    for key, reasons in excluded_by_key.items():
        for reason in reasons:
            text = str(reason)
            if not text.startswith("duplicate of "):
                continue
            problem = _duplicate_reference_error(
                key, text[len("duplicate of ") :], candidate_keys, excluded_by_key
            )
            if problem is not None:
                errors.append(problem)

    selected_moments: list[dict] | None = None
    if labels_ok and review_ok and n_clips_requested >= 0:
        try:
            labels = _load_json(labels_path)
            review = _load_json(review_path)
            labels_binding = labels.get("excerpts_sha256") if isinstance(labels, dict) else None
            if labels_binding is not None and labels_binding != manifest.get("excerpts_sha256"):
                errors.append("shortlist labels were written for a different analysis generation")
            rebuilt = build_shortlist(
                excerpts_rows,
                review,
                labels,
                n_clips_requested,
                doc.get("bindings") or {},
            )
        except Exception as exc:
            errors.append(f"shortlist cannot be reproduced: {exc}")
        else:
            if (
                rebuilt.get("selected") != selected
                or rebuilt.get("excluded") != excluded
                or rebuilt.get("counts") != counts
                or rebuilt.get("shortfall") != shortfall
            ):
                errors.append("shortlist does not match its inputs; re-run shortlist-apply")
            else:
                try:
                    selected_moments = [
                        {
                            "video_id": str(item["video_id"]),
                            "excerpt_index": int(item["excerpt_index"]),
                            "start_ms": source_ms(item["start_s"]),
                            "end_ms": source_ms(item["end_s"]),
                        }
                        for item in rebuilt["selected"]
                    ]
                except (TypeError, ValueError, KeyError):
                    selected_moments = None
                    errors.append("shortlist selected moments are malformed")

    request_fulfilled = bool(counts.get("request_fulfilled"))
    if "request_fulfilled" not in counts:
        request_fulfilled = n_clips_requested >= 0 and len(selected) >= n_clips_requested
    elif counts.get("request_fulfilled") != (len(selected) >= n_clips_requested if n_clips_requested >= 0 else False):
        errors.append("shortlist counts.request_fulfilled is inconsistent")

    return {
        "present": True,
        "n_selected": len(selected),
        "n_excluded": len(excluded),
        "shortfall": shortfall.get("count"),
        "request_fulfilled": request_fulfilled,
        "n_continuity_rejected_upstream": counts.get("n_continuity_rejected_upstream"),
        "shortfall_explanation": shortfall.get("explanation"),
        "selected_moments": selected_moments,
    }


def _check_export(
    run_dir: Path,
    root: Path | None,
    constraint: dict,
    analysis_manifest: dict,
    shortlist_summary: dict | None,
    errors: list[str],
    export_probe: ExportProbeFn,
    decode: DecodeFn,
    require_export: bool,
) -> dict | None:
    """Prove any published export: pointer -> manifest -> plan -> clips, with
    the shortlist re-derived by _check_shortlist as the trust anchor."""
    pointer_path = run_dir / "export.json"
    if not pointer_path.is_file():
        if require_export:
            errors.append("export verification requested but run_dir/export.json is missing")
        return None
    if root is None:
        errors.append("cannot resolve the project root for export verification")
        return None
    try:
        pointer = _load_json(pointer_path)
    except Exception as exc:
        errors.append(f"cannot load export.json pointer: {exc}")
        return None
    if not isinstance(pointer, dict) or pointer.get("schema_version") != POINTER_SCHEMA_VERSION:
        errors.append("export.json pointer is malformed or has an unsupported schema")
        return None
    theme = pointer.get("theme")
    manifest_rel = pointer.get("manifest_path")
    if not isinstance(theme, str) or not isinstance(manifest_rel, str):
        errors.append("export.json pointer is missing its theme or manifest path")
        return None
    root_resolved = Path(root).resolve()
    manifest_path = (root_resolved / manifest_rel).resolve()
    if not manifest_path.is_relative_to(root_resolved) or manifest_path.is_symlink():
        errors.append("export manifest path is unsafe (outside the project or a symlink)")
        return None
    if not manifest_path.is_file():
        errors.append(f"export manifest is missing: {manifest_rel}")
        return None
    if sha256_file(manifest_path) != pointer.get("manifest_sha256"):
        errors.append("export manifest changed since publication; re-export")
        return None
    try:
        doc = _load_json(manifest_path)
    except Exception as exc:
        errors.append(f"cannot load export manifest: {exc}")
        return None
    if not isinstance(doc, dict):
        errors.append("export manifest must be a JSON object")
        return None
    if doc.get("schema_version") != EXPORT_DOC_SCHEMA_VERSION:
        errors.append(f"unsupported export manifest schema {doc.get('schema_version')}")
    if doc.get("run_id") != run_dir.name or doc.get("theme") != theme:
        errors.append("export manifest does not belong to this run and theme")
    if doc.get("policy") != EXPORT_POLICY or doc.get("recipe") != EXPORT_RECIPE:
        errors.append("export manifest policy or recipe does not match the current exporter")
    cap = doc.get("max_height")
    if (
        isinstance(cap, bool)
        or not isinstance(cap, int)
        or not (EXPORT_CAP_MIN <= cap <= EXPORT_CAP_MAX)
    ):
        errors.append("export manifest has no valid max_height; re-export")
        cap = None
    shortlist_path = run_dir / "shortlist.json"
    if not shortlist_path.is_file():
        errors.append("export present but shortlist.json is missing; re-run shortlist-apply or re-export")
        return {"present": True, "theme": theme}
    if doc.get("shortlist_sha256") != sha256_file(shortlist_path):
        errors.append("export is stale for the current shortlist; re-export")
    if doc.get("generation_id") != analysis_manifest.get("generation_id"):
        errors.append("export was built from a different analysis generation; re-export")
    plan_path = manifest_path.parent / "plan.json"
    plan_doc = None
    if not plan_path.is_file() or plan_path.is_symlink():
        errors.append("export plan.json is missing; re-export")
    elif sha256_file(plan_path) != doc.get("plan_sha256"):
        errors.append("export plan.json changed since publication; re-export")
    else:
        try:
            plan_doc = _load_json(plan_path)
        except Exception as exc:
            errors.append(f"cannot load export plan.json: {exc}")
        if not isinstance(plan_doc, dict):
            errors.append("export plan.json must be a JSON object")
            plan_doc = None
    if plan_doc is not None:
        if plan_doc.get("schema_version") != EXPORT_DOC_SCHEMA_VERSION:
            errors.append(f"unsupported export plan schema {plan_doc.get('schema_version')}")
        if plan_doc.get("run_id") != run_dir.name or plan_doc.get("theme") != theme:
            errors.append("export plan.json does not belong to this run and theme")
        if plan_doc.get("policy") != EXPORT_POLICY or plan_doc.get("recipe") != EXPORT_RECIPE:
            errors.append("export plan policy or recipe does not match the current exporter")
        if plan_doc.get("generation_id") != doc.get("generation_id"):
            errors.append("export plan and manifest disagree on the analysis generation; re-export")
        if plan_doc.get("shortlist_sha256") != doc.get("shortlist_sha256"):
            errors.append("export plan and manifest disagree on the shortlist binding; re-export")
        if plan_doc.get("max_height") != cap:
            errors.append("export plan and manifest disagree on max_height; re-export")

    if shortlist_summary is None or shortlist_summary.get("selected_moments") is None:
        errors.append("export cannot be proven: the shortlist could not be re-derived")
        return {"present": True, "theme": theme}

    expected: dict[tuple[str, int], tuple[int, int]] = {}
    for item in shortlist_summary["selected_moments"]:
        expected[(str(item["video_id"]), int(item["excerpt_index"]))] = (
            int(item["start_ms"]),
            int(item["end_ms"]),
        )

    clips_raw = doc.get("clips")
    failed_raw = doc.get("failed")
    if not isinstance(clips_raw, list) or not isinstance(failed_raw, list):
        errors.append("export manifest is missing its clips or failed lists")
        return {"present": True, "theme": theme}

    clip_ids: list[tuple[str, int]] = []
    listed_names: set[str] = set()
    valid_clips: list[dict] = []
    for entry in clips_raw:
        if not isinstance(entry, dict):
            errors.append("export manifest contains a non-object clip entry")
            continue
        video_id = entry.get("video_id")
        index = entry.get("excerpt_index")
        if not isinstance(video_id, str) or isinstance(index, bool) or not isinstance(index, int):
            errors.append("export manifest contains a clip with an invalid identity")
            continue
        key = (video_id, index)
        if key in clip_ids:
            errors.append(f"export manifest lists clip {video_id}:{index} more than once")
            continue
        clip_ids.append(key)
        if key not in expected:
            errors.append(f"export contains a clip for {video_id}:{index}, which is not a selected moment")
            continue
        if (entry.get("start_ms"), entry.get("end_ms")) != expected[key]:
            errors.append(f"export clip {video_id}:{index} timestamps do not match the shortlist")
        file_field = entry.get("file")
        if not isinstance(file_field, str) or not file_field.startswith("clips/"):
            errors.append(f"export clip {video_id}:{index} has an unsafe or missing file path")
            continue
        name = file_field[len("clips/"):]
        if name in ("", ".", "..") or "/" in name or "\\" in name or ".." in name:
            errors.append(f"export clip {video_id}:{index} has an unsafe file name")
            continue
        listed_names.add(name)
        valid_clips.append(entry)

    failed_ids: list[tuple[str, int]] = []
    for entry in failed_raw:
        if not isinstance(entry, dict):
            errors.append("export manifest contains a non-object failure entry")
            continue
        video_id = entry.get("video_id")
        index = entry.get("excerpt_index")
        if not isinstance(video_id, str) or isinstance(index, bool) or not isinstance(index, int):
            errors.append("export manifest contains a failure with an invalid identity")
            continue
        key = (video_id, index)
        if key in failed_ids:
            errors.append(f"export manifest lists failure {video_id}:{index} more than once")
            continue
        failed_ids.append(key)
        if key not in expected:
            errors.append(f"export records a failure for {video_id}:{index}, which is not a selected moment")
        if not isinstance(entry.get("reason"), str) or not entry.get("reason"):
            errors.append(f"export failure {video_id}:{index} has no reason code")

    clip_set = set(clip_ids)
    failed_set = set(failed_ids)
    for key in sorted(expected, key=lambda item: (item[0], item[1])):
        in_clips = key in clip_set
        in_failed = key in failed_set
        if in_clips and in_failed:
            errors.append(f"export lists {key[0]}:{key[1]} as both a clip and a failure")
        elif not in_clips and not in_failed:
            errors.append(f"selected moment {key[0]}:{key[1]} has neither a clip nor a failure record")

    plan_moments: dict[tuple[str, int], dict] = {}
    acquisitions_checked = 0
    if plan_doc is not None:
        raw_moments = plan_doc.get("moments")
        if not isinstance(raw_moments, list):
            errors.append("export plan.json is missing its moments list")
        else:
            for item in raw_moments:
                if not isinstance(item, dict):
                    errors.append("export plan.json contains a non-object moment")
                    continue
                vid = item.get("video_id")
                idx = item.get("excerpt_index")
                if not isinstance(vid, str) or isinstance(idx, bool) or not isinstance(idx, int):
                    errors.append("export plan.json contains a moment with an invalid identity")
                    continue
                moment_key = (vid, idx)
                if moment_key in plan_moments:
                    errors.append(f"export plan.json lists moment {vid}:{idx} more than once")
                    continue
                plan_moments[moment_key] = item
        for key in sorted(expected, key=lambda item: (item[0], item[1])):
            if key not in plan_moments:
                errors.append(f"export plan.json is missing selected moment {key[0]}:{key[1]}")
        for key in sorted(plan_moments, key=lambda item: (item[0], item[1])):
            moment = plan_moments[key]
            if key not in expected:
                errors.append(f"export plan.json plans {key[0]}:{key[1]}, which is not a selected moment")
            elif (_int(moment.get("start_ms"), -1), _int(moment.get("end_ms"), -1)) != expected[key]:
                errors.append(f"export plan timestamps for {key[0]}:{key[1]} do not match the shortlist")

    counts = doc.get("counts") if isinstance(doc.get("counts"), dict) else {}
    requested = constraint.get("n_clips")
    if counts.get("requested") != requested:
        errors.append("export counts.requested does not match the constraint")
    if counts.get("selected") != len(expected):
        errors.append("export counts.selected does not match the shortlist")
    if counts.get("exported") != len(clip_ids):
        errors.append("export counts.exported does not match its clip entries")
    if counts.get("failed") != len(failed_ids):
        errors.append("export counts.failed does not match its failure entries")
    if counts.get("export_complete") != (not failed_ids):
        errors.append("export counts.export_complete is inconsistent")
    fulfilled = isinstance(requested, int) and len(clip_ids) >= requested
    if counts.get("request_fulfilled") != fulfilled:
        errors.append("export counts.request_fulfilled is inconsistent")

    clips_dir = manifest_path.parent / "clips"
    for entry in valid_clips:
        name = str(entry.get("file"))[len("clips/"):]
        key = (str(entry.get("video_id")), entry.get("excerpt_index"))
        path = clips_dir / name
        if path.is_symlink() or not path.is_file():
            errors.append(f"export clip file is missing or a symlink: clips/{name}")
            continue
        if entry.get("size_bytes") != path.stat().st_size:
            errors.append(f"export clip size changed: clips/{name}")
        try:
            actual = sha256_file(path)
        except OSError as exc:
            errors.append(f"cannot hash export clip clips/{name}: {exc}")
            continue
        if entry.get("sha256") != actual:
            errors.append(f"export clip hash changed: clips/{name}")
        moment = plan_moments.get(key)
        if moment is not None:
            if moment.get("status") != "ready":
                errors.append(
                    f"export contains a clip for {key[0]}:{key[1]}, which the plan marked {moment.get('status')}"
                )
            else:
                planned_spec = moment.get("spec") if isinstance(moment.get("spec"), dict) else {}
                if (
                    _int(planned_spec.get("width"), -1),
                    _int(planned_spec.get("height"), -1),
                ) != (_int(entry.get("width"), -1), _int(entry.get("height"), -1)):
                    errors.append(f"export clip dimensions do not match the plan: clips/{name}")
                if str(entry.get("format_id")) != str(planned_spec.get("format_id")):
                    errors.append(f"export clip format id does not match the plan: clips/{name}")
            span_pair = entry.get("acq_span_ms")
            if [
                _int(moment.get("acq_start_ms"), -1),
                _int(moment.get("acq_end_ms"), -1),
            ] != [
                _int(value, -1) if isinstance(span_pair, list) and len(span_pair) == 2 else -1
                for value in (span_pair or [None, None])
            ]:
                errors.append(f"export clip acquisition span does not match the plan: clips/{name}")
        try:
            rich = export_probe(path)
        except Exception as exc:
            errors.append(f"ffprobe failed for export clip clips/{name}: {exc}")
            continue
        k_ms = entry.get("mapping_k_ms")
        local_start = local_end = None
        if isinstance(k_ms, bool) or not isinstance(k_ms, int):
            errors.append(f"export clip {key[0]}:{key[1]} has no integer mapping key")
        elif cap is not None:
            local_start = (_int(entry.get("start_ms")) - k_ms) / 1000.0
            local_end = (_int(entry.get("end_ms")) - k_ms) / 1000.0
        if local_start is not None:
            problem = clip_media_problem(
                rich,
                local_start_s=local_start,
                local_end_s=local_end,
                width=_int(entry.get("width")),
                height=_int(entry.get("height")),
                max_height=cap,
            )
            if problem is not None:
                _code, message = problem
                errors.append(f"export clip {message}: clips/{name}")
            planned_ms = _int(entry.get("end_ms")) - _int(entry.get("start_ms"))
            if abs(_int(entry.get("duration_ms")) - planned_ms) > CLIP_DURATION_TOLERANCE_MS:
                errors.append(f"export clip manifest duration does not match the interval: clips/{name}")
        acq_sha = entry.get("acq_sha256")
        if not isinstance(acq_sha, str) or len(acq_sha) != 64:
            errors.append(f"export clip {key[0]}:{key[1]} has no acquisition digest")
        else:
            span_pair = entry.get("acq_span_ms")
            if (
                isinstance(span_pair, list)
                and len(span_pair) == 2
                and all(isinstance(value, int) and not isinstance(value, bool) for value in span_pair)
            ):
                acq_path = export_cache_path(
                    Path(root).resolve() / "data" / "cache" / "export",
                    key[0],
                    (span_pair[0] / 1000.0, span_pair[1] / 1000.0),
                    policy=f"{EXPORT_CACHE_POLICY}.{entry.get('format_id')}",
                )
                if acq_path.is_file() and not acq_path.is_symlink():
                    acquisitions_checked += 1
                    if sha256_file(acq_path) != acq_sha:
                        errors.append(f"acquisition cache content changed for clips/{name}")
                    acq_marker = export_marker_path(acq_path)
                    if acq_marker.is_file():
                        try:
                            marker_doc = _load_json(acq_marker)
                        except Exception:
                            marker_doc = None
                        if isinstance(marker_doc, dict):
                            if marker_doc.get("first_pts_ms") != entry.get("mapping_k_ms"):
                                errors.append(f"acquisition mapping key changed for clips/{name}")
                            if (marker_doc.get("width"), marker_doc.get("height")) != (
                                entry.get("width"),
                                entry.get("height"),
                            ):
                                errors.append(f"acquisition geometry changed for clips/{name}")
        try:
            decode(path)
        except Exception as exc:
            errors.append(f"decode failed for export clip clips/{name}: {exc}")
    if clips_dir.is_dir():
        for candidate in sorted(clips_dir.iterdir(), key=lambda item: item.name):
            if candidate.name in listed_names:
                continue
            if ".staging-" in candidate.name:
                errors.append(
                    "export publication is in progress or was interrupted "
                    f"(staging file present): clips/{candidate.name}"
                )
            else:
                errors.append(f"unlisted file in the export: clips/{candidate.name}")
    elif valid_clips:
        errors.append("export clips directory is missing")

    return {
        "present": True,
        "theme": theme,
        "exported": len(clip_ids),
        "failed": len(failed_ids),
        "export_complete": bool(counts.get("export_complete")),
        "request_fulfilled": bool(counts.get("request_fulfilled")),
        "acquisitions_checked": acquisitions_checked,
    }


def verify_run(
    run_dir: str | Path,
    analysis_dir: str | Path | None = None,
    probe_fn: ProbeFn | None = None,
    decode_fn: DecodeFn | None = None,
    root: str | Path | None = None,
    require_export: bool = False,
    export_probe_fn: ExportProbeFn | None = None,
) -> dict:
    with exclusive_run_lock(run_dir):
        return _verify_run_locked(
            run_dir, analysis_dir, probe_fn, decode_fn, root, require_export, export_probe_fn
        )


def _verify_run_locked(
    run_dir: str | Path,
    analysis_dir: str | Path | None,
    probe_fn: ProbeFn | None,
    decode_fn: DecodeFn | None,
    root: str | Path | None = None,
    require_export: bool = False,
    export_probe_fn: ExportProbeFn | None = None,
) -> dict:
    run_dir = Path(run_dir)
    probe = probe_fn or _default_probe
    decode = decode_fn or _default_decode
    export_probe = export_probe_fn or probe_export_coverage
    errors: list[str] = []
    warnings: list[str] = []

    paths = {
        "constraint.json": run_dir / "constraint.json",
        "candidates.json": run_dir / "candidates.json",
        "ranked.json": run_dir / "ranked.json",
        "excerpts.json": run_dir / "excerpts.json",
        "analysis_manifest.json": run_dir / "analysis_manifest.json",
    }
    for name, path in paths.items():
        if not path.is_file():
            errors.append(f"missing {name}")
    if errors:
        return _base_report(run_dir, errors, warnings)

    loaded = {}
    for name, path in paths.items():
        try:
            loaded[name] = _load_json(path)
        except Exception as exc:
            errors.append(f"cannot load {name}: {exc}")
    if errors:
        return _base_report(run_dir, errors, warnings)

    constraint = loaded["constraint.json"]
    candidates = loaded["candidates.json"]
    ranked = loaded["ranked.json"]
    excerpts = loaded["excerpts.json"]
    manifest = loaded["analysis_manifest.json"]
    if not isinstance(constraint, dict):
        errors.append("constraint.json must contain an object")
    if not isinstance(candidates, list):
        errors.append("candidates.json must contain an array")
    if not isinstance(ranked, list):
        errors.append("ranked.json must contain an array")
    if not isinstance(excerpts, list):
        errors.append("excerpts.json must contain an array")
    if not isinstance(manifest, dict):
        errors.append("analysis_manifest.json must contain an object")
    if errors:
        return _base_report(run_dir, errors, warnings)

    if manifest.get("schema_version") != ANALYSIS_SCHEMA_VERSION:
        errors.append(f"unsupported analysis manifest schema {manifest.get('schema_version')}")
    ranked_sha256 = sha256_file(paths["ranked.json"])
    constraint_sha256 = sha256_file(paths["constraint.json"])
    excerpts_sha256 = sha256_file(paths["excerpts.json"])
    if manifest.get("ranked_sha256") != ranked_sha256:
        errors.append("ranked.json changed after analysis")
    if manifest.get("constraint_sha256") != constraint_sha256:
        errors.append("constraint.json changed after analysis")
    if manifest.get("excerpts_sha256") != excerpts_sha256:
        errors.append("excerpts.json does not match analysis manifest")
    settings = _validate_settings(manifest, errors)
    if settings is not None and all(key in settings for key in REQUIRED_SETTINGS):
        expected_generation = analysis_generation_id(
            str(manifest.get("ranked_sha256") or ""),
            str(manifest.get("constraint_sha256") or ""),
            str(manifest.get("excerpts_sha256") or ""),
            settings,
        )
        if manifest.get("generation_id") != expected_generation:
            errors.append("analysis manifest generation_id is invalid")

    try:
        min_w = int(constraint.get("min_width") or 0)
        min_h = int(constraint.get("min_height") or 0)
        aspect_min = float(constraint.get("aspect_min") or 0)
        aspect_max = float(constraint.get("aspect_max") or 99)
        dur_min = float(constraint.get("duration_min_s") or 0)
        dur_max = float(constraint.get("duration_max_s") or 10**9)
        if not all(math.isfinite(value) for value in (aspect_min, aspect_max, dur_min, dur_max)):
            raise ValueError("non-finite constraint")
        if min_w < 0 or min_h < 0 or aspect_min > aspect_max or dur_min > dur_max:
            raise ValueError("invalid constraint range")
    except (TypeError, ValueError) as exc:
        errors.append(f"constraint fields are invalid: {exc}")
        min_w = min_h = 0
        aspect_min, aspect_max = 0.0, 99.0
        dur_min, dur_max = 0.0, 10**9

    candidate_ids: set[str] = set()
    for candidate in candidates:
        if not isinstance(candidate, dict):
            errors.append("candidates.json contains a non-object row")
            continue
        video_id = str(candidate.get("video_id") or "")
        if not video_id:
            errors.append("candidate row missing video_id")
            continue
        if video_id in candidate_ids:
            errors.append(f"duplicate candidate row {video_id}")
        candidate_ids.add(video_id)
        try:
            candidate_height = int(candidate.get("height") or 0)
            candidate_width = int(candidate.get("width") or 0)
        except (TypeError, ValueError):
            errors.append(f"candidate {video_id} has invalid dimensions")
            candidate_height = candidate_width = 0
        if candidate_height < min_h:
            errors.append(f"candidate {video_id} height below {min_h}")
        if candidate_width < min_w:
            errors.append(f"candidate {video_id} width below {min_w}")
        aspect = _finite_number(candidate.get("aspect"))
        if aspect is None or not (aspect_min <= aspect <= aspect_max):
            errors.append(f"candidate {video_id} aspect outside band")

    ranked_ids: set[str] = set()
    for item in ranked:
        if not isinstance(item, dict):
            continue
        video_id = str(item.get("video_id") or "")
        if video_id in ranked_ids:
            errors.append(f"duplicate ranked row {video_id}")
        ranked_ids.add(video_id)
        if video_id not in candidate_ids:
            errors.append(f"ranked {video_id} not in candidates")

    expected = _expected_rows(ranked, settings or {}, errors)
    actual_by_id: dict[str, dict] = {}
    for row in excerpts:
        if not isinstance(row, dict):
            errors.append("excerpts.json contains a non-object row")
            continue
        video_id = str(row.get("video_id") or "")
        if not video_id:
            errors.append("analysis row missing video_id")
            continue
        if video_id in actual_by_id:
            errors.append(f"duplicate analysis row {video_id}")
            continue
        actual_by_id[video_id] = row
        if video_id not in expected:
            errors.append(f"analysis row {video_id} not in ranked")
    for video_id in expected:
        if video_id not in actual_by_id:
            errors.append(f"missing analysis row {video_id}")

    allowed_root = Path(analysis_dir) if analysis_dir is not None else run_dir.parent / "analysis"
    allowed_root = allowed_root.resolve()
    referenced_media: dict[str, dict] = {}

    for video_id, expected_row in expected.items():
        row = actual_by_id.get(video_id)
        if row is None:
            continue
        if row.get("priority") != expected_row.get("priority"):
            errors.append(f"{video_id} analysis priority does not match ranked.json")
        if expected_row["kind"] == "skipped":
            if row.get("status") != "skipped":
                errors.append(f"{video_id} expected skipped analysis row")
            reason = row.get("skip_reason") or row.get("skipped")
            if reason != expected_row["reason"]:
                errors.append(f"{video_id} skip reason does not match plan")
            if row.get("ranges") or row.get("copies") or row.get("excerpts"):
                errors.append(f"{video_id} skipped row contains analysis artifacts")
            continue

        status = row.get("status")
        if status != "complete":
            errors.append(f"{video_id} analysis status {status}")
        if row.get("errors"):
            errors.append(f"{video_id} has {len(row['errors'])} range error(s)")
        if row.get("plan_mode") != expected_row["plan_mode"]:
            errors.append(f"{video_id} plan mode does not match recomputed plan")

        expected_keys = [canonical_span_ms(span) for span in expected_row["ranges"]]
        declared_plan_keys = [_span_key(span) for span in row.get("plan_ranges") or []]
        if declared_plan_keys != expected_keys:
            errors.append(f"{video_id} declared plan ranges do not match recomputed plan")

        outcomes = row.get("ranges")
        copies = row.get("copies")
        if not isinstance(outcomes, list):
            outcomes = []
            errors.append(f"{video_id} ranges must be an array")
        if not isinstance(copies, list):
            copies = []
            errors.append(f"{video_id} copies must be an array")
        outcome_keys = [_span_key(outcome.get("span")) for outcome in outcomes if isinstance(outcome, dict)]
        copy_keys = [_span_key(copy.get("span")) for copy in copies if isinstance(copy, dict)]
        if Counter(outcome_keys) != Counter(expected_keys):
            errors.append(f"{video_id} range outcomes do not match plan")
        completed_keys = [
            _span_key(outcome.get("span"))
            for outcome in outcomes
            if isinstance(outcome, dict) and outcome.get("status") == "complete"
        ]
        if Counter(copy_keys) != Counter(completed_keys) or Counter(copy_keys) != Counter(expected_keys):
            errors.append(f"{video_id} copies do not match completed ranges")

        copies_by_key: dict[str, dict] = {}
        for copy in copies:
            if not isinstance(copy, dict):
                errors.append(f"{video_id} copy is not an object")
                continue
            span = _finite_pair(copy.get("span"))
            cache_key = str(copy.get("cache_key") or "")
            path_text = str(copy.get("path") or "")
            if span is None:
                errors.append(f"{video_id} copy has invalid span")
                continue
            expected_cache_key = analysis_cache_path(allowed_root, video_id, span).name
            if cache_key != expected_cache_key:
                errors.append(f"{video_id} cache key does not match source span")
            if not path_text:
                errors.append(f"{video_id} copy missing media path")
                continue
            path = Path(path_text)
            if not path.is_absolute():
                errors.append(f"{video_id} media path is not absolute: {path}")
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(allowed_root):
                errors.append(f"{video_id} media path outside analysis cache: {path}")
                continue
            if resolved.name != expected_cache_key:
                errors.append(f"{video_id} media filename does not match cache key")
            size = copy.get("size_bytes")
            digest = copy.get("sha256")
            try:
                expected_size = int(size)
            except (TypeError, ValueError):
                errors.append(f"{video_id} copy missing valid size_bytes")
                expected_size = -1
            if not isinstance(digest, str) or len(digest) != 64:
                errors.append(f"{video_id} copy missing valid sha256")
                digest = ""
            previous = referenced_media.get(str(resolved))
            reference = {
                "video_id": video_id,
                "span": span,
                "cache_key": cache_key,
                "size_bytes": expected_size,
                "sha256": digest,
                "path": resolved,
            }
            if previous is not None and previous != reference:
                errors.append(f"conflicting duplicate media reference {resolved}")
            else:
                referenced_media[str(resolved)] = reference
            if cache_key in copies_by_key:
                errors.append(f"{video_id} duplicate analysis cache key {cache_key}")
            copies_by_key[cache_key] = copy

        for outcome in outcomes:
            if not isinstance(outcome, dict):
                continue
            if outcome.get("status") != "complete":
                errors.append(
                    f"{video_id} failed range {outcome.get('span')}: {outcome.get('error')}"
                )
            if outcome.get("attempt_errors"):
                warnings.append(
                    f"{video_id} range {outcome.get('span')} recovered after {len(outcome['attempt_errors'])} attempt error(s)"
                )

        excerpts_for_row = row.get("excerpts")
        if not isinstance(excerpts_for_row, list):
            excerpts_for_row = []
            errors.append(f"{video_id} excerpts must be an array")
        if not excerpts_for_row:
            warnings.append(f"{video_id} analysis completed with no usable excerpts")
        if bool(row.get("ready_for_shortlist")) != bool(excerpts_for_row):
            errors.append(f"{video_id} ready_for_shortlist disagrees with excerpts")
        for excerpt in excerpts_for_row:
            if not isinstance(excerpt, dict):
                errors.append(f"{video_id} excerpt is not an object")
                continue
            start = _finite_number(excerpt.get("start_s"))
            end = _finite_number(excerpt.get("end_s"))
            valid_coordinates = start is not None and end is not None
            if not valid_coordinates:
                errors.append(f"{video_id} excerpt has non-finite coordinates")
            else:
                assert start is not None and end is not None
                if start < 0 or end <= start:
                    errors.append(f"{video_id} excerpt has invalid interval {start}-{end}")
                length = end - start
                if length + 1e-6 < dur_min or length - 1e-6 > dur_max:
                    errors.append(f"{video_id} excerpt length {length:.2f}s outside {dur_min}-{dur_max}")
            span = _finite_pair(excerpt.get("analysis_span"))
            scene = _finite_pair(excerpt.get("source_scene"))
            if span is None:
                errors.append(f"{video_id} excerpt has invalid analysis span")
            elif not valid_coordinates or not (span[0] <= start <= end <= span[1]):
                errors.append(f"{video_id} excerpt outside analysis span")
            if scene is None:
                errors.append(f"{video_id} excerpt has invalid source scene")
            elif not valid_coordinates or not (scene[0] <= start <= end <= scene[1]):
                errors.append(f"{video_id} excerpt outside source scene")
            if span is not None and scene is not None and not (
                span[0] <= scene[0] <= scene[1] <= span[1]
            ):
                errors.append(f"{video_id} source scene outside analysis span")
            cache_key = str(excerpt.get("analysis_cache_key") or "")
            copy = copies_by_key.get(cache_key)
            if copy is None:
                errors.append(f"{video_id} excerpt references unknown analysis cache key {cache_key}")
            elif span is not None and _span_key(copy.get("span")) != _span_key(span):
                errors.append(f"{video_id} excerpt span does not match referenced media")

    for reference in referenced_media.values():
        path = reference["path"]
        if not path.is_file():
            errors.append(f"missing referenced media {path}")
            continue
        stat = path.stat()
        if stat.st_size <= 0:
            errors.append(f"empty referenced media {path}")
            continue
        if reference["size_bytes"] != stat.st_size:
            errors.append(f"media size changed {path}")
        actual_hash = sha256_file(path)
        if reference["sha256"] != actual_hash:
            errors.append(f"media hash changed {path}")
        _validate_marker(
            path,
            reference["video_id"],
            reference["span"],
            stat.st_size,
            actual_hash,
            errors,
        )
        try:
            media = probe(path)
        except Exception as exc:
            errors.append(f"ffprobe failed for {path}: {exc}")
            continue
        video_streams = int(media.get("video_streams") or 0)
        audio_streams = int(media.get("audio_streams") or 0)
        width = int(media.get("width") or 0)
        height = int(media.get("height") or 0)
        duration = _finite_number(media.get("duration_s"))
        if video_streams != 1:
            errors.append(f"expected exactly one video stream in {path}")
        if audio_streams != 0:
            errors.append(f"analysis media contains audio {path}")
        if width <= 0 or height <= 0:
            errors.append(f"invalid media dimensions {path}")
        elif height > 720:
            errors.append(f"analysis media exceeds 720p {path}")
        else:
            aspect = width / height
            if not (aspect_min <= aspect <= aspect_max):
                errors.append(f"analysis media aspect outside band {path}")
        expected_duration = reference["span"][1] - reference["span"][0]
        if duration is None or duration <= 0 or abs(duration - expected_duration) > MEDIA_DURATION_TOLERANCE_S:
            errors.append(f"media duration does not match span {path}")
        try:
            decode(path)
        except Exception as exc:
            errors.append(f"decode failed for {path}: {exc}")

    shortlist_summary = _check_shortlist(run_dir, excerpts, constraint, manifest, errors)
    export_summary = _check_export(
        run_dir,
        Path(root) if root is not None else None,
        constraint,
        manifest,
        shortlist_summary,
        errors,
        export_probe,
        decode,
        require_export,
    )

    n_excerpt_clips = sum(
        len(row.get("excerpts") or []) for row in actual_by_id.values() if isinstance(row, dict)
    )
    n_sources_without_excerpts = sum(
        1
        for video_id, expected_row in expected.items()
        if expected_row["kind"] != "skipped"
        and not (actual_by_id.get(video_id) or {}).get("excerpts")
    )
    report = _base_report(run_dir, errors, warnings)
    report.update(
        {
            "n_candidates": len(candidates),
            "n_ranked": len(ranked),
            "n_excerpt_rows": len(excerpts),
            "n_excerpts": n_excerpt_clips,
            "n_media": len(referenced_media),
            "min_width": min_w,
            "min_height": min_h,
            "ready_for_shortlist": not errors and n_excerpt_clips > 0,
            "all_analyzed_sources_have_excerpts": (
                not errors and n_excerpt_clips > 0 and n_sources_without_excerpts == 0
            ),
            "n_sources_without_excerpts": n_sources_without_excerpts,
            "shortlist": shortlist_summary,
            "export": export_summary,
        }
    )
    return report
