"""One command that walks the existing stages and stops only for a real handoff.

It calls the stage functions already in this package. It does not re-decide
which clips pass. A frozen plan skips the planner. Jev is called only when
the config turns on jev_rank (discovery) or jev_note_check (strip notes) and
the jev_post port is supplied (docs/JEV.md).
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from scenery_brief_clips.brief import (
    canonical_json_hash,
    validate_brief,
    validate_query_plan,
)
from scenery_brief_clips.cli import _brief_constraint_from_brief
from scenery_brief_clips.config import load_project_config
from scenery_brief_clips.continuity import DEFAULT_CONTINUITY_DETECTOR, continuity_settings_from_config
from scenery_brief_clips.jev import API_KEY_ENV, DiscoveryJudge, JevClient, JevSettings, apply_note_check
from scenery_brief_clips.pipeline import run_dry
from scenery_brief_clips.pipeline_analyze import analyze_run
from scenery_brief_clips.rank import rank_run
from scenery_brief_clips.review import review_run
from scenery_brief_clips.shortlist import shortlist_apply_run
from scenery_brief_clips.store import MetadataCache, write_json_atomic, write_run
from scenery_brief_clips.verify import verify_run
from scenery_brief_clips.vision import apply_scores_run_detailed
from scenery_brief_clips.yt import is_youtube_block
from scenery_brief_clips.vision_wire import (
    label_ranked_tiles,
    label_review_strips,
    load_vision_wire,
)

STATE_NAME = "runner_state.json"
INFLIGHT_NAME = "runner_inflight.json"
LOCK_NAME = "runner.lock"

DISCOVERY_KEYS = (
    "max_search_results",
    "max_metadata_fetches",
    "min_width",
    "min_height",
    "aspect_min",
    "aspect_max",
)
RANK_KEYS = ("max_rank_videos", "max_tiles")
ANALYZE_KEYS = (
    "max_analyze_videos",
    "max_analysis_s",
    "continuity_enabled",
    "continuity_sample_fps",
    "continuity_max_adjacent_hamming",
    "continuity_max_endpoint_hamming",
    "continuity_max_adjacent_color",
    "continuity_max_endpoint_color",
)
ANALYZE_ENV = ("SCENERY_DETECT_FRAME_SKIP", "SCENERY_CONTINUITY_DECODE_WIDTH")
EXPORT_KEYS = ("export_max_height",)
# Wall-clock budget for one runner invocation (config run_deadline_s). When it
# is spent the runner stops before the next stage (analyze also stops starting
# new spans) and returns status "deadline"; rerunning the same command resumes
# with a fresh budget and reuses completed stages and cached spans.
DEFAULT_RUN_DEADLINE_S = 3 * 3600.0

EXTERNAL_STAGES = {
    "discover",
    "rank",
    "label_tiles",
    "label_strips",
    "analyze",
    "shortlist_review",
    "export",
}

STAGE_ORDER = (
    "discover",
    "rank",
    "agree_vision",
    "label_tiles",
    "apply_scores",
    "analyze",
    "verify_review",
    "shortlist_review",
    "label_strips",
    "shortlist_apply",
    "agree_export",
    "export",
    "verify_export",
)


@dataclass
class Ports:
    """Substitutions for outside systems. None means do not call the network."""

    yt: Any = None
    sleep_fn: Callable[[float], None] | None = None
    rank_fetcher: Callable[[str], bytes] | None = None
    fetch_span: Callable | None = None
    detect_fn: Callable | None = None
    invalidate_span: Callable | None = None
    planner_caller: Callable | None = None
    planner_wire: Any = None  # PlannerWire for planner_caller (recorded in provenance)
    prefetch_spans: Callable | None = None
    tile_caller: Callable | None = None
    strip_caller: Callable | None = None
    verify_probe: Callable | None = None
    verify_decode: Callable | None = None
    export_probe: Callable | None = None
    clock: Callable[[], float] | None = None
    # Jev decisions POST (jev.default_post); used only when the config enables
    # jev_rank or jev_note_check.
    jev_post: Callable | None = None


@dataclass
class _Counters:
    model_calls: int = 0
    tokens: int | None = None
    saw_unreported_usage: bool = False
    calls: list[str] = field(default_factory=list)
    # Labeling calls run concurrently (vision_wire.vision_workers()).
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False, compare=False)


def advance(
    root: str | Path,
    *,
    brief: str | Path | None = None,
    plan: str | Path | None = None,
    run_dir: str | Path | None = None,
    vision_agree: bool = False,
    allow_export: bool = False,
    judgments: str | Path | dict | None = None,
    acknowledge_uncertain: str | None = None,
    config_path: str | Path | None = None,
    theme: str | None = None,
    ports: Ports | None = None,
) -> dict:
    root = Path(root).resolve()
    ports = ports or Ports()
    clock = ports.clock or time.monotonic
    config = load_project_config(root, Path(config_path) if config_path else None)
    brief_doc = _load_json(Path(brief)) if brief else None
    plan_doc = _load_json(Path(plan)) if plan else None
    if brief_doc is not None:
        validate_brief(brief_doc)
        config = _config_with_brief_export_cap(config, brief_doc)
    if plan_doc is not None:
        if brief_doc is None:
            raise ValueError("a frozen plan requires the brief it was bound to")
        validate_query_plan(plan_doc, brief_doc)
    wire = load_vision_wire(root)
    model_id = {"backend": wire.backend, "model": wire.model}

    if run_dir is None:
        run_dir = _new_run_dir(root)
    else:
        run_dir = Path(run_dir)
        if not run_dir.is_absolute():
            run_dir = (root / run_dir).resolve()
        run_dir.mkdir(parents=True, exist_ok=True)

    lock = _try_lock(run_dir)
    if lock is None:
        return {
            "status": "locked",
            "stage": None,
            "run_dir": str(run_dir),
            "missing": "another runner holds this run",
            "how_to_supply": "wait until the other runner exits, then resume with the same --run-dir",
            "jev": _jev_status(config),
        }
    try:
        return _advance_locked(
            root=root,
            run_dir=run_dir,
            brief_doc=brief_doc,
            plan_doc=plan_doc,
            config=config,
            wire=wire,
            model_id=model_id,
            vision_agree=vision_agree,
            allow_export=allow_export,
            judgments=_load_judgments(judgments),
            acknowledge_uncertain=acknowledge_uncertain,
            theme=theme,
            ports=ports,
            clock=clock,
        )
    finally:
        fcntl.flock(lock.fileno(), fcntl.LOCK_UN)
        lock.close()


def _config_with_brief_export_cap(config: dict, brief: dict) -> dict:
    """The brief's export_max_height is the requested cap; config may only lower it.

    Left untouched when the result equals what the config already resolves to,
    so runs made before briefs could ask for 1080p keep their export binding.
    """
    from scenery_brief_clips.export import config_with_requested_export_cap

    return config_with_requested_export_cap(config, int(brief["export_max_height"]))


def _advance_locked(**kw) -> dict:
    root: Path = kw["root"]
    run_dir: Path = kw["run_dir"]
    config: dict = kw["config"]
    wire = kw["wire"]
    model_id = kw["model_id"]
    ports: Ports = kw["ports"]
    clock = kw["clock"]
    state = _load_state(run_dir)
    state["jev"] = _jev_status(config)
    state.setdefault("completed", {})
    state.setdefault("timing", {"stages": [], "retries": 0, "waiting_for_input_s": 0.0})
    state["vision_model"] = model_id
    if kw["vision_agree"]:
        state.setdefault("approvals", {})["vision"] = dict(model_id)
    if kw["allow_export"]:
        state.setdefault("approvals", {})["export"] = True
    _invalidate(state, _bindings(kw["brief_doc"], kw["plan_doc"], config, model_id))
    counters = _Counters()
    started_wait = state.get("pause_started_at")
    if started_wait is not None:
        state["timing"]["waiting_for_input_s"] = round(
            state["timing"].get("waiting_for_input_s", 0.0) + max(0.0, clock() - float(started_wait)),
            6,
        )
        state["pause_started_at"] = None

    inflight = _read_inflight(run_dir)
    if inflight is not None:
        stage = inflight["stage"]
        if kw["acknowledge_uncertain"] == stage:
            _clear_inflight(run_dir)
            _drop_from(state, stage)
            state["timing"]["retries"] = int(state["timing"].get("retries") or 0) + 1
        elif stage in EXTERNAL_STAGES:
            return _pause(
                state,
                run_dir,
                clock,
                status="recovery",
                stage=stage,
                missing=f"stage {stage} was interrupted and its output is not valid",
                how_to_supply=(
                    f"inspect {run_dir}, fix or remove the partial {stage} output, "
                    f"then resume with acknowledge_uncertain={stage}"
                ),
            )

    bindings = _bindings(kw["brief_doc"], kw["plan_doc"], config, model_id)
    deadline_s = float(config.get("run_deadline_s", DEFAULT_RUN_DEADLINE_S))
    invocation_started = clock()
    for stage in STAGE_ORDER:
        saved = state["completed"].get(stage)
        if saved and saved.get("outputs") != _output_hashes(run_dir, stage):
            if kw["acknowledge_uncertain"] == stage:
                _drop_from(state, stage)
                state["timing"]["retries"] += 1
            else:
                return _pause(state, run_dir, clock, status="recovery", stage=stage,
                    missing="completed stage outputs changed or lack a content checkpoint",
                    how_to_supply=f"inspect {run_dir}, then resume with acknowledge_uncertain={stage}")
        if _reusable(state, run_dir, stage, bindings):
            _record(state, stage, "reused", 0.0, 0, None)
            continue
        handoff = _handoff(stage, state, kw, ports, wire)
        if handoff is not None:
            return _pause(state, run_dir, clock, **handoff)
        remaining_s = deadline_s - (clock() - invocation_started)
        if remaining_s <= 0:
            return _deadline(state, run_dir, clock, stage, deadline_s)
        kw["span_deadline"] = time.monotonic() + remaining_s
        began = clock()
        _write_inflight(run_dir, stage, bindings.get(stage))
        try:
            outcome = _run_stage(stage, root, run_dir, kw, ports, wire, counters)
            _note_fallback(state, ports)
        except Exception as exc:
            _note_fallback(state, ports)
            _clear_inflight(run_dir)
            _record(state, stage, "failed", clock() - began, counters.model_calls, _tokens(counters))
            state["failed_stage"] = stage
            _save(run_dir, state, clock)
            if is_youtube_block(exc):
                return _blocked(state, run_dir, stage, str(exc), ports)
            return _result(state, run_dir, status="failed", stage=stage, error=str(exc))
        elapsed = clock() - began
        if outcome.get("failed"):
            _clear_inflight(run_dir)
            _record(state, stage, "failed", elapsed, counters.model_calls, _tokens(counters))
            state["failed_stage"] = stage
            _save(run_dir, state, clock)
            if outcome.get("deadline"):
                return _deadline(state, run_dir, clock, stage, deadline_s, saved=True)
            if outcome.get("blocked"):
                return _blocked(state, run_dir, stage, outcome.get("error") or f"{stage} failed", ports)
            return _result(
                state,
                run_dir,
                status="failed",
                stage=stage,
                error=outcome.get("error") or f"{stage} failed",
            )
        if not _outputs_ok(run_dir, stage):
            return _pause(
                state,
                run_dir,
                clock,
                status="recovery",
                stage=stage,
                missing=f"stage {stage} finished without the required files",
                how_to_supply=(
                    f"inspect {run_dir}, then resume with acknowledge_uncertain={stage} "
                    "only after the partial output is understood"
                ),
            )
        state["completed"][stage] = {
            "binding": bindings.get(stage), "outputs": _output_hashes(run_dir, stage)}
        # apply-scores intentionally rewrites rank's output; bind both owners.
        if stage == "apply_scores" and "rank" in state["completed"]:
            state["completed"]["rank"]["outputs"] = _output_hashes(run_dir, "rank")
        _record(state, stage, "executed", elapsed, counters.model_calls, _tokens(counters))
        _save(run_dir, state, clock)
        _clear_inflight(run_dir)
        counters.model_calls = 0
        counters.tokens = None
        counters.saw_unreported_usage = False

    _save(run_dir, state, clock)
    return _result(state, run_dir, status="completed", stage="verify_export")


def _run_stage(stage, root, run_dir, kw, ports: Ports, wire, counters: _Counters) -> dict:
    if stage == "discover":
        return _discover(root, run_dir, kw, ports, counters)
    if stage == "rank":
        rank_run(
            run_dir,
            root / "data" / "cache" / "metadata",
            fetcher=ports.rank_fetcher,
            max_videos=int(kw["config"].get("max_rank_videos", 10)),
            max_tiles=int(kw["config"].get("max_tiles", 12)),
            tiles_root=root / "data" / "cache" / "tiles",
        )
        return {}
    if stage == "agree_vision":
        return {}
    if stage == "label_tiles":
        return _label(run_dir, wire, ports.tile_caller, counters, kind="tile", judgments=kw["judgments"])
    if stage == "apply_scores":
        apply_scores_run_detailed(run_dir, run_dir / "vision_scores.json")
        return {}
    if stage == "analyze":
        rows = analyze_run(
            run_dir,
            cache_dir=root / "data" / "cache" / "analysis",
            fetch_span=ports.fetch_span,
            detect_fn=ports.detect_fn,
            max_videos=int(kw["config"].get("max_analyze_videos", 1)),
            max_analysis_s=float(kw["config"].get("max_analysis_s", 120.0)),
            invalidate_span=ports.invalidate_span,
            continuity_settings=continuity_settings_from_config(kw["config"]),
            deadline=kw.get("span_deadline"),
            prefetch_spans=ports.prefetch_spans,
            format_for=_analysis_format_for(root, kw["config"]),
        )
        bad = [row for row in rows if row.get("status") in {"partial", "failed", "invalid"}]
        if bad:
            deadline_hit = any(
                (outcome.get("stage") == "deadline")
                for row in bad
                for outcome in row.get("ranges") or []
            )
            blocked = any(
                is_youtube_block(item.get("error"))
                for row in bad
                for outcome in row.get("ranges") or []
                for item in outcome.get("attempt_errors") or []
            )
            return {"failed": True, "deadline": deadline_hit, "blocked": blocked,
                    "error": f"analyze failed for {bad[0].get('video_id')}"}
        return {}
    if stage == "verify_review":
        report = verify_run(
            run_dir,
            analysis_dir=root / "data" / "cache" / "analysis",
            probe_fn=ports.verify_probe,
            decode_fn=ports.verify_decode,
            root=root,
        )
        write_json_atomic(run_dir / "verify_review.json", report)
        if not report.get("ok") or not report.get("ready_for_shortlist"):
            return {"failed": True, "error": "not ready for shortlist"}
        return {}
    if stage == "shortlist_review":
        review_run(run_dir)
        return {}
    if stage == "label_strips":
        outcome = _label(run_dir, wire, ports.strip_caller, counters, kind="strip", judgments=kw["judgments"])
        captured = (kw["judgments"] or {}).get("strip_scores") is not None
        settings = JevSettings.from_config(kw["config"])
        if outcome or captured or not settings.note_check:
            return outcome
        scores = json.loads((run_dir / "shortlist_scores.json").read_text(encoding="utf-8"))
        scores, report = apply_note_check(kw["brief_doc"] or {}, scores, _jev_client(root, settings, ports), settings)
        write_json_atomic(run_dir / "jev_notes.json", report)
        write_json_atomic(run_dir / "shortlist_scores.json", scores)
        return {}
    if stage == "shortlist_apply":
        shortlist_apply_run(run_dir, run_dir / "shortlist_scores.json")
        return {}
    if stage == "agree_export":
        return {}
    if stage == "export":
        from scenery_brief_clips.export import export_run

        manifest = export_run(
            run_dir,
            root,
            theme=kw["theme"],
            allow_export=True,
            yt=ports.yt,
            config=kw["config"],
        )
        if (manifest.get("counts") or {}).get("failed"):
            blocked = any(
                is_youtube_block(entry.get("detail")) or any(map(is_youtube_block, entry.get("attempt_errors") or []))
                for entry in manifest.get("failed") or []
            )
            return {"failed": True, "blocked": blocked, "error": "export recorded a failed moment"}
        return {}
    if stage == "verify_export":
        report = verify_run(
            run_dir,
            analysis_dir=root / "data" / "cache" / "analysis",
            probe_fn=ports.verify_probe,
            decode_fn=ports.verify_decode,
            export_probe_fn=ports.export_probe,
            root=root,
            require_export=True,
            trusted_decodes=_review_decodes(run_dir),
        )
        write_json_atomic(run_dir / "verify_export.json", report)
        if not report.get("ok"):
            return {"failed": True, "error": "export verify failed"}
        return {}
    raise RuntimeError(f"unknown stage {stage}")


def _discover(root, run_dir, kw, ports: Ports, counters: _Counters) -> dict:
    brief = kw["brief_doc"]
    plan = kw["plan_doc"]
    if plan is None:
        from scenery_brief_clips.planner import PlannerError, plan_queries

        if ports.planner_caller is None:
            return {"failed": True, "error": "planner caller missing"}
        counters.calls.append("planner")
        try:
            plan, provenance = plan_queries(brief, ports.planner_wire, caller=ports.planner_caller)
        except PlannerError as exc:
            counters.model_calls += exc.model_calls
            counters.saw_unreported_usage = True
            return {"failed": True, "error": str(exc)}
        counters.model_calls += provenance["model_calls"]
        _note_usage(counters, getattr(ports.planner_caller, "last_usage", None))
    else:
        provenance = {
            "schema_version": "brief_plan_provenance_v1",
            "instruction_version": "search_query_planner_v1",
            "plan_sha256": canonical_json_hash(plan),
            "source": "frozen_plan_file",
        }
    queries = [item["query"] for item in plan["queries"]]
    if not queries:
        return {"failed": True, "error": "empty_query_plan"}
    config = kw["config"]
    constraint, _limits = _brief_constraint_from_brief(brief, {
        "max_results": config.get("max_search_results"),
        "max_metadata": config.get("max_metadata_fetches"),
    })
    constraint = replace(constraint,
        min_width=max(constraint.min_width, config.get("min_width", constraint.min_width)),
        min_height=max(constraint.min_height, config.get("min_height", constraint.min_height)),
        aspect_min=max(constraint.aspect_min, config.get("aspect_min", constraint.aspect_min)),
        aspect_max=min(constraint.aspect_max, config.get("aspect_max", constraint.aspect_max)))
    if constraint.aspect_min > constraint.aspect_max:
        raise ValueError("configured aspect band does not overlap the brief")
    cache = MetadataCache(root / "data" / "cache" / "metadata")
    jev_settings = JevSettings.from_config(config)
    judge = DiscoveryJudge(brief, _jev_client(root, jev_settings, ports), jev_settings) if jev_settings.rank else None
    result = run_dry(
        constraint,
        yt=ports.yt,
        cache=cache,
        sleep_fn=ports.sleep_fn or (lambda _seconds: None),
        queries=queries,
        # Rank uses only the first max_rank_videos candidates. Not part of the
        # discover binding, so older runs resume without re-searching; a later
        # rise of max_rank_videos ranks only what discovery kept.
        max_candidates=int(config.get("max_rank_videos", 10)),
        judge=judge,
    )
    if result.stopped_reason in {"search_error", "metadata_errors"}:
        blocked = any(is_youtube_block(line) for line in result.log_lines)
        return {"failed": True, "blocked": blocked, "error": result.stopped_reason}
    log_text = "\n".join(result.log_lines) + ("\n" if result.log_lines else "")
    written = write_run(
        run_dir.parent,
        constraint=constraint,
        candidates=result.candidates,
        rejected=result.rejected,
        log_text=log_text,
        run_id=run_dir.name,
    )
    if written.resolve() != run_dir.resolve():
        return {"failed": True, "error": f"discovery wrote {written}, expected {run_dir}"}
    candidates_json = json.loads((run_dir / "candidates.json").read_text(encoding="utf-8"))
    rejected_json = json.loads((run_dir / "rejected.json").read_text(encoding="utf-8"))
    for record in candidates_json + rejected_json:
        record["visual_status"] = "unverified"
        record["acceptance_level"] = "metadata_only"
    discovery = {
        "schema_version": "brief_discovery_v1",
        "brief_sha256": canonical_json_hash(brief),
        # The requested cap, so standalone `export`/`analyze` apply it too.
        "export_max_height": brief["export_max_height"],
        "max_candidates": int(config.get("max_rank_videos", 10)),
        "query_plan": plan,
        "plan_provenance": provenance,
        "attempted_queries": result.queries,
        "counts": {
            "attempted_queries": len(result.queries),
            "candidates": len(result.candidates),
            "rejected": len(result.rejected),
        },
        "stopped_reason": result.stopped_reason,
        "allow_download": False,
        "candidates": candidates_json,
        "rejected": rejected_json,
    }
    if judge is not None:
        discovery["jev_rank"] = judge.report()
    write_json_atomic(run_dir / "discovery.json", discovery)
    write_json_atomic(run_dir / "candidates.json", candidates_json)
    write_json_atomic(run_dir / "rejected.json", rejected_json)
    return {}


def _review_decodes(run_dir: Path) -> dict[str, str]:
    """Analysis media verify_review strictly decoded ({path: sha256}).

    verify_review.json is a completed stage output whose hash the runner
    re-checks on every invocation, so verify_export may skip decoding bytes
    it lists (verify re-hashes each file first). Anything unreadable -> {}.
    """
    try:
        report = json.loads((run_dir / "verify_review.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    decoded = report.get("decoded_media") if isinstance(report, dict) and report.get("ok") else None
    if not isinstance(decoded, dict):
        return {}
    return {str(k): v for k, v in decoded.items() if isinstance(v, str) and len(v) == 64}


def _label(run_dir, wire, caller, counters: _Counters, *, kind: str, judgments: dict | None) -> dict:
    captured = None
    if judgments:
        captured = judgments.get("tile_scores" if kind == "tile" else "strip_scores")
    if captured is not None:
        name = "vision_scores.json" if kind == "tile" else "shortlist_scores.json"
        write_json_atomic(run_dir / name, captured)
        return {}
    if caller is None:
        return {"failed": True, "error": f"{kind} caller missing"}
    wrapped = _wrap_caller(caller, counters, kind)
    if kind == "tile":
        result = label_ranked_tiles(run_dir, wire, caller=wrapped)
        if result["failures"]:
            return {"failed": True, "error": "tile label failed"}
        write_json_atomic(run_dir / "vision_scores.json", result["scores"])
        return {}
    result = label_review_strips(run_dir, wire, caller=wrapped)
    if result["failures"]:
        return {"failed": True, "error": "strip label failed"}
    write_json_atomic(run_dir / "shortlist_scores.json", result["scores"])
    return {}


def _wrap_caller(caller, counters: _Counters, seam: str):
    def wrapped(wire, image_path, prompt):
        with counters.lock:
            counters.calls.append(seam)
            counters.model_calls += 1
        text = caller(wire, image_path, prompt)
        with counters.lock:
            _note_usage(counters, getattr(caller, "last_usage", None))
        return text

    return wrapped


def _note_usage(counters: _Counters, usage) -> None:
    if not isinstance(usage, dict) or usage.get("total_tokens") is None:
        counters.saw_unreported_usage = True
        counters.tokens = None
        return
    if counters.saw_unreported_usage:
        counters.tokens = None
        return
    counters.tokens = int(counters.tokens or 0) + int(usage["total_tokens"])


def _tokens(counters: _Counters):
    if counters.model_calls == 0:
        return None
    if counters.saw_unreported_usage or counters.tokens is None:
        return None
    return counters.tokens


def _handoff(stage, state, kw, ports: Ports, wire) -> dict | None:
    model = {"backend": wire.backend, "model": wire.model, "plain": wire.as_public_dict().get("plain")}
    if stage == "discover" and ports.yt is None:
        return {
            "status": "paused",
            "stage": stage,
            "missing": "search results are not available",
            "how_to_supply": "resume with a frozen plan and captured search results supplied through the runner ports",
        }
    if stage == "discover" and kw["plan_doc"] is None and ports.planner_caller is None:
        return {
            "status": "paused",
            "stage": stage,
            "missing": "a frozen plan or the live planner",
            "how_to_supply": "pass --plan PLAN.json, or --live-planner to call the planner named in planner.yaml.",
        }
    if stage == "rank" and ports.rank_fetcher is None:
        return {
            "status": "paused",
            "stage": stage,
            "missing": "storyboard fetcher",
            "how_to_supply": "supply a storyboard fetcher. The runner will not fetch images from the network by itself in this milestone.",
        }
    if stage == "agree_vision":
        saved = (state.get("approvals") or {}).get("vision")
        if saved != {"backend": wire.backend, "model": wire.model}:
            return {
                "status": "paused",
                "stage": stage,
                "missing": "vision agreement",
                "how_to_supply": "resume with --vision-agree after accepting the model named here",
                "vision_model": model,
            }
    if stage == "label_tiles":
        judgments = kw["judgments"] or {}
        if judgments.get("tile_scores") is None and ports.tile_caller is None:
            return {
                "status": "paused",
                "stage": stage,
                "missing": "tile judgments",
                "how_to_supply": "pass --judgments with tile_scores, or supply a tile caller. The live vision wire is not called.",
                "vision_model": model,
            }
    if stage == "analyze" and (ports.fetch_span is None or ports.detect_fn is None):
        return {
            "status": "paused",
            "stage": stage,
            "missing": "analysis fetch or scene detector",
            "how_to_supply": "supply fetch_span and detect_fn. The runner will not download video by itself in this milestone.",
        }
    if stage == "label_strips":
        judgments = kw["judgments"] or {}
        if judgments.get("strip_scores") is None and ports.strip_caller is None:
            return {
                "status": "paused",
                "stage": stage,
                "missing": "strip judgments",
                "how_to_supply": "pass --judgments with strip_scores, or supply a strip caller. The live vision wire is not called.",
                "vision_model": model,
            }
    if stage == "agree_export":
        if not (state.get("approvals") or {}).get("export"):
            return {
                "status": "paused",
                "stage": stage,
                "missing": "export allowance",
                "how_to_supply": "resume with --allow-export. Nothing is downloaded until then.",
            }
    if stage == "export" and ports.yt is None:
        return {
            "status": "paused",
            "stage": stage,
            "missing": "export fetcher",
            "how_to_supply": "supply an export fetcher. The runner will not call yt-dlp by itself in this milestone.",
        }
    return None


def _bindings(brief, plan, config, model_id) -> dict:
    discovery = {
        "brief": canonical_json_hash(brief) if brief is not None else None,
        "plan": canonical_json_hash(plan) if plan is not None else None,
        "config": {key: config.get(key) for key in DISCOVERY_KEYS},
    }
    rank = {**discovery, "config": {key: config.get(key) for key in RANK_KEYS}}
    # Jev enters a binding only when switched on, so runs made without it keep
    # their bindings and resume without re-searching or relabeling.
    jev_settings = JevSettings.from_config(config)
    if jev_settings.rank_binding() is not None:
        discovery["jev_rank"] = jev_settings.rank_binding()
        rank["jev_rank"] = jev_settings.rank_binding()
    from scenery_brief_clips.vision_wire import brief_prompt
    vision = {"model": model_id, "tile_prompt": brief_prompt("tile", ""),
              "strip_prompt": brief_prompt("strip", ""), "label_policy": "vision_label_v1"}
    analyze = {
        "config": {key: config.get(key) for key in ANALYZE_KEYS},
        "env": {key: os.environ.get(key) for key in ANALYZE_ENV},
    }
    # Bound only when the effective detector is not legacy: runs with an explicit
    # legacy config keep their old analyze binding, while runs analyzed before
    # blend became the default are re-analyzed on resume.
    detector = config.get("continuity_detector", DEFAULT_CONTINUITY_DETECTOR)
    if detector != "legacy":
        analyze["config"]["continuity_detector"] = detector
    export = {"config": {key: config.get(key) for key in EXPORT_KEYS}}
    strips = vision if jev_settings.note_binding() is None else {**vision, "jev_note": jev_settings.note_binding()}
    encoded = {
        "discover": _hash(discovery),
        "rank": _hash(rank),
        "agree_vision": _hash(vision),
        "label_tiles": _hash(vision),
        "apply_scores": _hash(vision),
        "analyze": _hash(analyze),
        "verify_review": _hash(analyze),
        "shortlist_review": _hash(analyze),
        "label_strips": _hash(strips),
        "shortlist_apply": _hash(strips),
        "agree_export": _hash(export),
        "export": _hash(export),
        "verify_export": _hash(export),
    }
    return encoded


def _invalidate(state, bindings) -> None:
    completed = state.get("completed") or {}
    drop = False
    for stage in STAGE_ORDER:
        saved = (completed.get(stage) or {}).get("binding")
        if saved is not None and saved != bindings.get(stage):
            drop = True
        if drop:
            completed.pop(stage, None)
    state["completed"] = completed


def _reusable(state, run_dir, stage, bindings) -> bool:
    # A saved report is not proof that referenced media is still intact.
    if stage == "verify_export":
        return False
    saved = (state.get("completed") or {}).get(stage) or {}
    return saved.get("binding") == bindings.get(stage) and _outputs_ok(run_dir, stage)


def _drop_from(state, stage):
    for name in STAGE_ORDER[STAGE_ORDER.index(stage):]:
        state["completed"].pop(name, None)


def _output_names(stage):
    return {
        "discover": ["candidates.json", "constraint.json", "discovery.json"],
        "rank": ["ranked.json"],
        "agree_vision": [],
        "label_tiles": ["vision_scores.json"],
        "apply_scores": ["ranked.json"],
        "analyze": ["excerpts.json", "analysis_manifest.json"],
        "verify_review": ["verify_review.json"],
        "shortlist_review": ["review.json"],
        "label_strips": ["shortlist_scores.json"],
        "shortlist_apply": ["shortlist.json"],
        "agree_export": [],
        "export": ["export.json"],
        "verify_export": ["verify_export.json"],
    }[stage]


def _output_hashes(run_dir, stage):
    try:
        return {name: hashlib.sha256((run_dir / name).read_bytes()).hexdigest()
                for name in _output_names(stage)}
    except OSError:
        return None


def _outputs_ok(run_dir: Path, stage: str) -> bool:
    try:
        for name in _output_names(stage):
            value = _load_json(run_dir / name)
            if not isinstance(value, (dict, list)):
                return False
            if stage in {"verify_review", "verify_export"} and not value.get("ok"):
                return False
        return True
    except (OSError, ValueError, AttributeError):
        return False


def _analysis_format_for(root, config):
    from scenery_brief_clips.export import analysis_format_id, resolve_max_height

    max_height = resolve_max_height(config)
    return lambda row: analysis_format_id(root, row, max_height)


def _deadline(state, run_dir, clock, stage, deadline_s, saved=False) -> dict:
    if not saved:
        _save(run_dir, state, clock)
    return _result(
        state,
        run_dir,
        status="deadline",
        stage=stage,
        missing=f"run_deadline_s={deadline_s:g} was spent before {stage} finished",
        how_to_supply=(
            "rerun the same command with the same --run-dir; completed stages and "
            "cached analysis spans are reused and the new invocation gets a fresh budget"
        ),
    )


def _note_fallback(state, ports) -> None:
    """Record in the run state that a YouTube call went through the fallback."""
    if getattr(ports.yt, "fallback_used", False):
        state["youtube_fallback_used"] = True


def _blocked(state, run_dir, stage, error, ports=None) -> dict:
    """YouTube refused this host (bot check). Not a pipeline fault: retrying
    now only prolongs the block."""
    fallback = getattr(getattr(ports, "yt", None), "fallback", None)
    if getattr(fallback, "configured", False):
        advice = ("The configured fallback (youtube_cookies_file / youtube_proxy) was refused "
                  "too: refresh the cookies or change the proxy, or wait.")
    else:
        advice = ("No fallback is configured; the owner may opt in with youtube_cookies_file "
                  "or youtube_proxy in config.")
    return _result(
        state,
        run_dir,
        status="blocked",
        stage=stage,
        error=error,
        missing="YouTube is refusing this host (\"Sign in to confirm you're not a bot\")",
        how_to_supply=(
            "wait (the block usually lifts within hours), then rerun the same command with "
            "the same --run-dir; completed stages and cached analysis spans are reused. "
            + advice
        ),
    )


def _pause(state, run_dir, clock, **fields) -> dict:
    state["pause_started_at"] = clock()
    _record(state, fields.get("stage") or "paused", "paused", 0.0, 0, None)
    _save(run_dir, state, clock)
    return _result(state, run_dir, **fields)


def _record(state, stage, status, elapsed, model_calls, tokens) -> None:
    state["timing"]["stages"].append(
        {
            "stage": stage,
            "status": status,
            "elapsed_s": round(max(0.0, elapsed), 6),
            "model_calls": model_calls,
            "tokens": tokens,
        }
    )


def _save(run_dir, state, clock) -> None:
    state["active_execution_s"] = round(
        sum(
            item["elapsed_s"]
            for item in state["timing"]["stages"]
            if item["status"] in {"executed", "failed"}
        ),
        6,
    )
    state["saved_at"] = clock()
    write_json_atomic(run_dir / STATE_NAME, state)


def _result(state, run_dir, **fields) -> dict:
    payload = {
        "status": fields.get("status"),
        "stage": fields.get("stage"),
        "run_dir": str(run_dir),
        "missing": fields.get("missing"),
        "how_to_supply": fields.get("how_to_supply"),
        "vision_model": fields.get("vision_model"),
        "error": fields.get("error"),
        "jev": state.get("jev") or _jev_status({}),
        "timing": state.get("timing"),
    }
    if state.get("youtube_fallback_used"):
        payload["youtube_fallback_used"] = True
    return payload


def _jev_status(config) -> dict:
    settings = JevSettings.from_config(config)
    if not (settings.rank or settings.note_check):
        return {"enabled": False, "reason": "config_off"}
    return {"enabled": True, "rank": settings.rank, "note_check": settings.note_check}


def _jev_client(root: Path, settings: JevSettings, ports: Ports) -> JevClient:
    return JevClient(api_key=os.environ.get(API_KEY_ENV, ""), cache_dir=root / "data" / "cache" / "jev",
                     settings=settings, post=ports.jev_post)


def _load_state(run_dir: Path) -> dict:
    path = run_dir / STATE_NAME
    if not path.is_file():
        return {"schema_version": "runner_state_v1", "completed": {}, "approvals": {}, "timing": {"stages": [], "retries": 0, "waiting_for_input_s": 0.0}}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_inflight(run_dir: Path):
    path = run_dir / INFLIGHT_NAME
    if not path.is_file():
        return None
    return json.loads(path.read_text(encoding="utf-8"))


def _write_inflight(run_dir: Path, stage: str, binding) -> None:
    write_json_atomic(run_dir / INFLIGHT_NAME, {"stage": stage, "binding": binding})


def _clear_inflight(run_dir: Path) -> None:
    path = run_dir / INFLIGHT_NAME
    if path.is_file():
        path.unlink()


def _try_lock(run_dir: Path):
    handle = (run_dir / LOCK_NAME).open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        return None
    return handle


def _new_run_dir(root: Path) -> Path:
    from datetime import datetime, timezone

    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / "data" / "runs" / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def _load_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _load_judgments(value):
    if value is None:
        return None
    if isinstance(value, dict):
        return value
    return json.loads(Path(value).read_text(encoding="utf-8"))


def _hash(value) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
