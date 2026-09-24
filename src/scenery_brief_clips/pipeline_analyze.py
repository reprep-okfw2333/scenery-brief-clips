from __future__ import annotations

import hashlib
import json
import math
import os
import time
import threading
import uuid
from collections.abc import Callable
from pathlib import Path

from scenery_brief_clips.analysis_cache import (
    ANALYSIS_CACHE_POLICY,
    analysis_cache_path,
    canonical_span_seconds,
    sha256_file,
)
from scenery_brief_clips.analyze import analysis_plan, duration_settings_from_constraint, excerpts_from_scenes
from scenery_brief_clips.continuity import ContinuitySettings, gate_excerpt
from scenery_brief_clips.run_lock import exclusive_run_lock

KEEP_PRIORITIES = {"promising", "uncertain"}
ANALYSIS_SCHEMA_VERSION = 3


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def analysis_generation_id(
    ranked_sha256: str,
    constraint_sha256: str,
    excerpts_sha256: str,
    settings: dict,
) -> str:
    return _sha256_bytes(
        json.dumps(
            {
                "ranked_sha256": ranked_sha256,
                "constraint_sha256": constraint_sha256,
                "excerpts_sha256": excerpts_sha256,
                "settings": settings,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    )


def _json_bytes(payload) -> bytes:
    return (json.dumps(payload, indent=2, ensure_ascii=False) + "\n").encode("utf-8")


def _write_fsync(path: Path, data: bytes) -> None:
    with path.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())


def _publish_generation(run_dir: Path, excerpts_bytes: bytes, manifest_bytes: bytes) -> None:
    token = uuid.uuid4().hex
    excerpts_path = run_dir / "excerpts.json"
    manifest_path = run_dir / "analysis_manifest.json"
    excerpts_tmp = run_dir / f".excerpts.{token}.tmp"
    manifest_tmp = run_dir / f".analysis_manifest.{token}.tmp"
    try:
        _write_fsync(excerpts_tmp, excerpts_bytes)
        _write_fsync(manifest_tmp, manifest_bytes)
        os.replace(excerpts_tmp, excerpts_path)
        os.replace(manifest_tmp, manifest_path)
        directory_fd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        excerpts_tmp.unlink(missing_ok=True)
        manifest_tmp.unlink(missing_ok=True)


def _clamp_source_scenes(
    local_scenes: list[tuple[float, float]],
    span: tuple[float, float],
) -> list[tuple[float, float]]:
    offset, span_end = span
    out = []
    for scene in local_scenes:
        if not isinstance(scene, (tuple, list)) or len(scene) != 2:
            raise ValueError(f"invalid detector scene: {scene!r}")
        local_start, local_end = float(scene[0]), float(scene[1])
        if not (math.isfinite(local_start) and math.isfinite(local_end)):
            raise ValueError(f"non-finite detector scene: {scene!r}")
        if local_end <= local_start:
            raise ValueError(f"invalid detector scene interval: {scene!r}")
        source_start = max(offset, offset + local_start)
        source_end = min(span_end, offset + local_end)
        if source_end > source_start:
            out.append((source_start, source_end))
    return out


def _safe_invalidate(
    invalidate_span: Callable[[Path], None] | None,
    path: Path,
    attempt_errors: list[dict],
    attempt: int,
) -> None:
    if invalidate_span is None:
        return
    try:
        invalidate_span(path)
    except Exception as exc:
        attempt_errors.append(
            {
                "attempt": attempt,
                "stage": "invalidate",
                "error": str(exc),
            }
        )


def _failed_video_row(
    item: dict,
    video_id: str,
    priority: str,
    stage: str,
    error: Exception | str,
) -> dict:
    failure = {
        "span": [],
        "status": "failed",
        "stage": stage,
        "attempts": 0,
        "attempt_errors": [{"attempt": 0, "stage": stage, "error": str(error)}],
        "error": str(error),
    }
    return {
        "video_id": video_id,
        "title": item.get("title"),
        "priority": priority,
        "status": "failed",
        "plan_mode": "invalid",
        "plan_ranges": [],
        "ranges": [failure],
        "copies": [],
        "excerpts": [],
        "errors": [failure],
        "ready_for_shortlist": False,
    }


def analyze_run(
    run_dir: str | Path,
    cache_dir: str | Path,
    fetch_span: Callable,
    detect_fn: Callable,
    max_videos: int = 5,
    max_analysis_s: float = 600.0,
    pad_s: float = 2.0,
    min_scene_len_s: float = 0.5,
    acquisition_attempts: int = 2,
    invalidate_span: Callable[[Path], None] | None = None,
    continuity_settings: ContinuitySettings | None = None,
    continuity_gate: Callable | None = None,
) -> list[dict]:
    with exclusive_run_lock(Path(run_dir)):
        return _analyze_run_locked(
            run_dir=run_dir,
            cache_dir=cache_dir,
            fetch_span=fetch_span,
            detect_fn=detect_fn,
            max_videos=max_videos,
            max_analysis_s=max_analysis_s,
            pad_s=pad_s,
            min_scene_len_s=min_scene_len_s,
            acquisition_attempts=acquisition_attempts,
            invalidate_span=invalidate_span,
            continuity_settings=continuity_settings,
            continuity_gate=continuity_gate,
        )


def _analyze_workers() -> int:
    raw = os.environ.get("SCENERY_ANALYZE_WORKERS", "4").strip()
    try:
        return max(1, int(raw))
    except ValueError:
        return 4


class _PhaseTimer:
    """Accumulate wall seconds per named phase; optional JSON dump via env.

    Phase totals are summed work time (can exceed wall when parallel).
    """

    def __init__(self) -> None:
        self.totals: dict[str, float] = {}
        self.counts: dict[str, int] = {}
        self.spans: list[dict] = []
        self.t0 = time.perf_counter()
        self._lock = threading.Lock()

    def add(self, name: str, seconds: float, **extra) -> None:
        with self._lock:
            self.totals[name] = self.totals.get(name, 0.0) + float(seconds)
            self.counts[name] = self.counts.get(name, 0) + 1
            if extra:
                self.spans.append({"phase": name, "seconds": round(float(seconds), 4), **extra})

    def dump(self, path: str | Path | None = None) -> dict:
        elapsed = time.perf_counter() - self.t0
        payload = {
            "elapsed_s": round(elapsed, 4),
            "workers": _analyze_workers(),
            "phases": {
                name: {
                    "total_s": round(total, 4),
                    "count": self.counts.get(name, 0),
                    "pct": round(100.0 * total / elapsed, 2) if elapsed > 0 else 0.0,
                }
                for name, total in sorted(self.totals.items())
            },
            "span_events": self.spans,
        }
        out = path or os.environ.get("SCENERY_ANALYZE_PROFILE")
        if out:
            Path(out).write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        return payload


def _process_span(
    *,
    video_id: str,
    span: tuple[float, float],
    dest: Path,
    fetch_span: Callable,
    detect_fn: Callable,
    invalidate_span: Callable[[Path], None] | None,
    gate_fn: Callable,
    continuity: ContinuitySettings,
    target_s: float,
    min_s: float,
    max_s: float,
    min_scene_len_s: float,
    acquisition_attempts: int,
    timer: _PhaseTimer | None,
) -> dict:
    """Acquire one span, detect scenes, run continuity; return outcome payload."""
    attempt_errors: list[dict] = []
    last_stage = "acquire"
    last_error = "analysis acquisition failed"

    for attempt in range(1, acquisition_attempts + 1):
        path: Path | None = None
        try:
            t0 = time.perf_counter()
            path = Path(fetch_span(video_id, dest, span))
            if timer is not None:
                timer.add("acquire", time.perf_counter() - t0, video_id=video_id, span=list(span))
            if not path.is_file() or path.stat().st_size <= 0:
                raise RuntimeError("analysis acquisition returned no nonempty file")
        except Exception as exc:
            last_stage = "acquire"
            last_error = str(exc)
            attempt_errors.append({"attempt": attempt, "stage": last_stage, "error": last_error})
            continue

        try:
            t0 = time.perf_counter()
            local_scenes = detect_fn(path, min_scene_len_s=min_scene_len_s)
            source_scenes = _clamp_source_scenes(local_scenes, span)
            if timer is not None:
                timer.add("detect", time.perf_counter() - t0, video_id=video_id, span=list(span))
        except Exception as exc:
            last_stage = "detect"
            last_error = str(exc)
            attempt_errors.append({"attempt": attempt, "stage": last_stage, "error": last_error})
            _safe_invalidate(invalidate_span, path, attempt_errors, attempt)
            continue

        try:
            t_hash = time.perf_counter()
            copy = {
                "path": str(path),
                "span": list(span),
                "cache_key": dest.name,
                "size_bytes": path.stat().st_size,
                "sha256": sha256_file(path),
            }
            if timer is not None:
                timer.add("hash", time.perf_counter() - t_hash, video_id=video_id, span=list(span))
            generated = []
            continuity_rejected = []
            for excerpt in excerpts_from_scenes(
                source_scenes,
                target_s=target_s,
                min_s=min_s,
                max_s=max_s,
            ):
                if not continuity.enabled:
                    generated.append(
                        {
                            "start_s": excerpt.start_s,
                            "end_s": excerpt.end_s,
                            "source_scene": list(excerpt.source_scene),
                            "analysis_span": list(span),
                            "analysis_cache_key": dest.name,
                        }
                    )
                    continue
                t_gate = time.perf_counter()
                decision = gate_fn(
                    excerpt,
                    video_path=path,
                    analysis_span=span,
                    target_s=target_s,
                    min_s=min_s,
                    max_s=max_s,
                    settings=continuity,
                )
                if timer is not None:
                    timer.add(
                        "continuity",
                        time.perf_counter() - t_gate,
                        video_id=video_id,
                        span=list(span),
                    )
                if decision.action == "reject":
                    continuity_rejected.append(decision.as_reject_record())
                    continue
                record = {
                    "start_s": decision.start_s,
                    "end_s": decision.end_s,
                    "source_scene": list(excerpt.source_scene),
                    "analysis_span": list(span),
                    "analysis_cache_key": dest.name,
                    "continuity": decision.as_excerpt_meta(),
                }
                generated.append(record)
        except Exception as exc:
            last_stage = "record"
            last_error = str(exc)
            attempt_errors.append({"attempt": attempt, "stage": last_stage, "error": last_error})
            _safe_invalidate(invalidate_span, path, attempt_errors, attempt)
            continue

        return {
            "ok": True,
            "copy": copy,
            "excerpts": generated,
            "outcome": {
                **copy,
                "status": "complete",
                "attempts": attempt,
                "attempt_errors": attempt_errors,
                "continuity_rejected": continuity_rejected,
            },
        }

    return {
        "ok": False,
        "outcome": {
            "span": list(span),
            "cache_key": dest.name,
            "status": "failed",
            "stage": last_stage,
            "attempts": acquisition_attempts,
            "attempt_errors": attempt_errors,
            "error": last_error,
        },
    }


def _analyze_run_locked(
    run_dir: str | Path,
    cache_dir: str | Path,
    fetch_span: Callable,
    detect_fn: Callable,
    max_videos: int = 5,
    max_analysis_s: float = 600.0,
    pad_s: float = 2.0,
    min_scene_len_s: float = 0.5,
    acquisition_attempts: int = 2,
    invalidate_span: Callable[[Path], None] | None = None,
    continuity_settings: ContinuitySettings | None = None,
    continuity_gate: Callable | None = None,
) -> list[dict]:
    from concurrent.futures import ThreadPoolExecutor, as_completed

    run_dir = Path(run_dir)
    cache_dir = Path(cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    ranked_path = run_dir / "ranked.json"
    constraint_path = run_dir / "constraint.json"
    ranked_bytes = ranked_path.read_bytes()
    constraint_bytes = constraint_path.read_bytes()
    ranked_sha256 = _sha256_bytes(ranked_bytes)
    constraint_sha256 = _sha256_bytes(constraint_bytes)
    ranked = json.loads(ranked_bytes)
    constraint = json.loads(constraint_bytes)
    target_s, min_s, max_s = duration_settings_from_constraint(constraint)
    acquisition_attempts = max(1, int(acquisition_attempts))
    continuity = (continuity_settings or ContinuitySettings()).validated()
    gate_fn = continuity_gate or gate_excerpt
    timer = _PhaseTimer()
    workers = _analyze_workers()

    # First pass: plan each video and collect span jobs (preserve ranked order).
    video_plans: list[dict] = []
    analyzed = 0
    for item in ranked:
        video_id = str(item.get("video_id") or "")
        priority = str(item.get("priority") or "")
        if priority not in KEEP_PRIORITIES:
            video_plans.append(
                {
                    "kind": "skipped",
                    "row": {
                        "video_id": video_id,
                        "title": item.get("title"),
                        "priority": priority,
                        "status": "skipped",
                        "skip_reason": priority or "ineligible",
                        "skipped": priority or "ineligible",
                        "excerpts": [],
                        "ranges": [],
                        "errors": [],
                        "ready_for_shortlist": False,
                    },
                }
            )
            continue
        if analyzed >= max_videos:
            video_plans.append(
                {
                    "kind": "skipped",
                    "row": {
                        "video_id": video_id,
                        "title": item.get("title"),
                        "priority": priority,
                        "status": "skipped",
                        "skip_reason": "max_videos",
                        "skipped": "max_videos",
                        "excerpts": [],
                        "ranges": [],
                        "errors": [],
                        "ready_for_shortlist": False,
                    },
                }
            )
            continue

        analyzed += 1
        try:
            duration_s = float(item.get("duration_s") or 0.0)
            t0 = time.perf_counter()
            plan = analysis_plan(
                duration_s=duration_s,
                windows=item.get("windows") or [],
                max_analysis_s=max_analysis_s,
                pad_s=pad_s,
            )
            canonical_ranges = [canonical_span_seconds(span) for span in plan.ranges]
            timer.add("plan", time.perf_counter() - t0, video_id=video_id)
        except Exception as exc:
            video_plans.append(
                {
                    "kind": "failed",
                    "row": _failed_video_row(item, video_id, priority, "plan", exc),
                }
            )
            continue

        jobs = []
        early_outcomes = []
        if not canonical_ranges:
            early_outcomes.append(
                {
                    "span": [],
                    "status": "failed",
                    "stage": "plan",
                    "attempts": 0,
                    "attempt_errors": [
                        {"attempt": 0, "stage": "plan", "error": "analysis plan produced no ranges"}
                    ],
                    "error": "analysis plan produced no ranges",
                }
            )
        for span in canonical_ranges:
            try:
                dest = analysis_cache_path(cache_dir, video_id, span)
            except Exception as exc:
                early_outcomes.append(
                    {
                        "span": list(span),
                        "status": "failed",
                        "stage": "plan",
                        "attempts": 0,
                        "attempt_errors": [{"attempt": 0, "stage": "plan", "error": str(exc)}],
                        "error": str(exc),
                    }
                )
                continue
            jobs.append({"span": span, "dest": dest})

        video_plans.append(
            {
                "kind": "work",
                "item": item,
                "video_id": video_id,
                "priority": priority,
                "plan": plan,
                "canonical_ranges": canonical_ranges,
                "jobs": jobs,
                "early_outcomes": early_outcomes,
            }
        )

    # Flatten span jobs with stable keys for ordered reassembly.
    flat_jobs: list[tuple[int, int, dict]] = []
    for v_idx, plan in enumerate(video_plans):
        if plan["kind"] != "work":
            continue
        for s_idx, job in enumerate(plan["jobs"]):
            flat_jobs.append((v_idx, s_idx, job))

    results: dict[tuple[int, int], dict] = {}

    def _run_job(v_idx: int, s_idx: int, job: dict) -> tuple[tuple[int, int], dict]:
        plan = video_plans[v_idx]
        result = _process_span(
            video_id=plan["video_id"],
            span=job["span"],
            dest=job["dest"],
            fetch_span=fetch_span,
            detect_fn=detect_fn,
            invalidate_span=invalidate_span,
            gate_fn=gate_fn,
            continuity=continuity,
            target_s=target_s,
            min_s=min_s,
            max_s=max_s,
            min_scene_len_s=min_scene_len_s,
            acquisition_attempts=acquisition_attempts,
            timer=timer,
        )
        return (v_idx, s_idx), result

    t_pool = time.perf_counter()
    if workers == 1 or len(flat_jobs) <= 1:
        for v_idx, s_idx, job in flat_jobs:
            key, result = _run_job(v_idx, s_idx, job)
            results[key] = result
    else:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(_run_job, v_idx, s_idx, job) for v_idx, s_idx, job in flat_jobs]
            for fut in as_completed(futures):
                key, result = fut.result()
                results[key] = result
    timer.add("pool_wait_wall", time.perf_counter() - t_pool)

    # Assemble rows in original ranked order; spans in plan order.
    rows: list[dict] = []
    for v_idx, plan in enumerate(video_plans):
        if plan["kind"] != "work":
            rows.append(plan["row"])
            continue

        excerpts_out: list[dict] = []
        copies: list[dict] = []
        outcomes: list[dict] = list(plan["early_outcomes"])
        for s_idx, _job in enumerate(plan["jobs"]):
            result = results[(v_idx, s_idx)]
            if result.get("ok"):
                outcomes.append(result["outcome"])
                copies.append(result["copy"])
                excerpts_out.extend(result["excerpts"])
            else:
                outcomes.append(result["outcome"])

        completed = sum(1 for outcome in outcomes if outcome["status"] == "complete")
        failed = sum(1 for outcome in outcomes if outcome["status"] == "failed")
        if completed and not failed:
            status = "complete"
        elif completed:
            status = "partial"
        else:
            status = "failed"
        errors = [outcome for outcome in outcomes if outcome["status"] == "failed"]
        rejected = []
        for outcome in outcomes:
            rejected.extend(outcome.get("continuity_rejected") or [])
        rows.append(
            {
                "video_id": plan["video_id"],
                "title": plan["item"].get("title"),
                "priority": plan["priority"],
                "status": status,
                "plan_mode": plan["plan"].mode,
                "plan_ranges": [list(r) for r in plan["canonical_ranges"]],
                "ranges": outcomes,
                "copies": copies,
                "excerpts": excerpts_out,
                "continuity_rejected": rejected,
                "errors": errors,
                "ready_for_shortlist": status == "complete" and bool(excerpts_out),
            }
        )

    if _sha256_bytes(ranked_path.read_bytes()) != ranked_sha256 or _sha256_bytes(
        constraint_path.read_bytes()
    ) != constraint_sha256:
        raise RuntimeError("analysis inputs changed during analysis; generation was not published")

    settings = {
        "max_videos": max_videos,
        "max_analysis_s": max_analysis_s,
        "pad_s": pad_s,
        "min_scene_len_s": min_scene_len_s,
        "target_duration_s": target_s,
        "duration_min_s": min_s,
        "duration_max_s": max_s,
        "acquisition_attempts": acquisition_attempts,
        "cache_policy": ANALYSIS_CACHE_POLICY,
        "analyze_workers": workers,
        **continuity.as_manifest(),
    }
    excerpts_bytes = _json_bytes(rows)
    excerpts_sha256 = _sha256_bytes(excerpts_bytes)
    generation_id = analysis_generation_id(
        ranked_sha256=ranked_sha256,
        constraint_sha256=constraint_sha256,
        excerpts_sha256=excerpts_sha256,
        settings=settings,
    )
    manifest = {
        "schema_version": ANALYSIS_SCHEMA_VERSION,
        "generation_id": generation_id,
        "ranked_sha256": ranked_sha256,
        "constraint_sha256": constraint_sha256,
        "excerpts_sha256": excerpts_sha256,
        "settings": settings,
    }
    _publish_generation(run_dir, excerpts_bytes, _json_bytes(manifest))
    timer.dump()
    return rows
