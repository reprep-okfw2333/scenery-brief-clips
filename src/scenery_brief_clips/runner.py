"""One command that walks the existing stages and stops only for a real handoff.

It calls the stage functions already in this package. It does not re-decide
which clips pass. A frozen plan skips the planner. Jev is never called.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from scenery_brief_clips.brief import (
    canonical_json_hash,
    validate_brief,
    validate_query_plan,
)
from scenery_brief_clips.cli import _brief_constraint_from_brief
from scenery_brief_clips.config import load_project_config
from scenery_brief_clips.continuity import continuity_settings_from_config
from scenery_brief_clips.pipeline import run_dry
from scenery_brief_clips.pipeline_analyze import analyze_run
from scenery_brief_clips.rank import rank_run
from scenery_brief_clips.review import review_run
from scenery_brief_clips.shortlist import shortlist_apply_run
from scenery_brief_clips.store import MetadataCache, write_json_atomic, write_run
from scenery_brief_clips.verify import verify_run
from scenery_brief_clips.vision import apply_scores_run_detailed
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
    tile_caller: Callable | None = None
    strip_caller: Callable | None = None
    verify_probe: Callable | None = None
    verify_decode: Callable | None = None
    export_probe: Callable | None = None
    clock: Callable[[], float] | None = None


@dataclass
class _Counters:
    model_calls: int = 0
    tokens: int | None = None
    saw_unreported_usage: bool = False
    calls: list[str] = field(default_factory=list)


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
            "jev": _jev_disabled(),
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


def _advance_locked(**kw) -> dict:
    root: Path = kw["root"]
    run_dir: Path = kw["run_dir"]
    config: dict = kw["config"]
    wire = kw["wire"]
    model_id = kw["model_id"]
    ports: Ports = kw["ports"]
    clock = kw["clock"]
    state = _load_state(run_dir)
    state["jev"] = _jev_disabled()
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
        if _outputs_ok(run_dir, stage):
            state["completed"][stage] = {"binding": inflight.get("binding")}
            _clear_inflight(run_dir)
        elif kw["acknowledge_uncertain"] == stage:
            _clear_inflight(run_dir)
            state["completed"].pop(stage, None)
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
    for stage in STAGE_ORDER:
        if _reusable(state, run_dir, stage, bindings):
            _record(state, stage, "reused", 0.0, 0, None)
            continue
        handoff = _handoff(stage, state, kw, ports, wire)
        if handoff is not None:
            return _pause(state, run_dir, clock, **handoff)
        began = clock()
        _write_inflight(run_dir, stage, bindings.get(stage))
        try:
            outcome = _run_stage(stage, root, run_dir, kw, ports, wire, counters)
        except Exception as exc:
            _clear_inflight(run_dir)
            _record(state, stage, "failed", clock() - began, counters.model_calls, _tokens(counters))
            state["failed_stage"] = stage
            _save(run_dir, state, clock)
            return _result(state, run_dir, status="failed", stage=stage, error=str(exc))
        elapsed = clock() - began
        if outcome.get("failed"):
            _clear_inflight(run_dir)
            _record(state, stage, "failed", elapsed, counters.model_calls, _tokens(counters))
            state["failed_stage"] = stage
            _save(run_dir, state, clock)
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
        _clear_inflight(run_dir)
        state["completed"][stage] = {"binding": bindings.get(stage)}
        _record(state, stage, "executed", elapsed, counters.model_calls, _tokens(counters))
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
        )
        bad = [row for row in rows if row.get("status") in {"partial", "failed", "invalid"}]
        if bad:
            return {"failed": True, "error": f"analyze failed for {bad[0].get('video_id')}"}
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
        return _label(run_dir, wire, ports.strip_caller, counters, kind="strip", judgments=kw["judgments"])
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
            return {"failed": True, "error": "export recorded a failed moment"}
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
        from scenery_brief_clips.planner import plan_queries

        if ports.planner_caller is None:
            return {"failed": True, "error": "planner caller missing"}
        counters.calls.append("planner")
        counters.model_calls += 1
        _note_usage(counters, getattr(ports.planner_caller, "last_usage", None))
        plan, provenance = plan_queries(brief, None, caller=ports.planner_caller)
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
    constraint, _limits = _brief_constraint_from_brief(brief, {})
    cache = MetadataCache(root / "data" / "cache" / "metadata")
    result = run_dry(
        constraint,
        yt=ports.yt,
        cache=cache,
        sleep_fn=ports.sleep_fn or (lambda _seconds: None),
        queries=queries,
    )
    if result.stopped_reason in {"search_error", "metadata_errors"}:
        return {"failed": True, "error": result.stopped_reason}
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
    write_json_atomic(run_dir / "discovery.json", discovery)
    write_json_atomic(run_dir / "candidates.json", candidates_json)
    write_json_atomic(run_dir / "rejected.json", rejected_json)
    return {}


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
    wrapped = _wrap_caller(caller, counters, "planner" if False else ("tile" if kind == "tile" else "strip"))
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
        counters.calls.append(seam)
        counters.model_calls += 1
        text = caller(wire, image_path, prompt)
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
            "missing": "a frozen plan or a planner caller",
            "how_to_supply": "pass --plan PLAN.json, or supply a planner caller. The live planner wire is not called.",
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
    vision = {"model": model_id}
    analyze = {
        "config": {key: config.get(key) for key in ANALYZE_KEYS},
        "env": {key: os.environ.get(key) for key in ANALYZE_ENV},
    }
    export = {"config": {key: config.get(key) for key in EXPORT_KEYS}}
    encoded = {
        "discover": _hash(discovery),
        "rank": _hash(rank),
        "agree_vision": _hash(vision),
        "label_tiles": _hash(vision),
        "apply_scores": _hash(vision),
        "analyze": _hash(analyze),
        "verify_review": _hash(analyze),
        "shortlist_review": _hash(analyze),
        "label_strips": _hash(vision),
        "shortlist_apply": _hash(vision),
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
    if (state.get("approvals") or {}).get("vision") != bindings and False:
        pass
    saved_model = (state.get("approvals") or {}).get("vision")
    current = json.loads(json.dumps(saved_model)) if saved_model else None
    # vision binding is a hash, so compare the stored approval to the live model
    # via the caller. _invalidate receives hashes only; the live check is in _handoff.
    state["completed"] = completed
    del current


def _reusable(state, run_dir, stage, bindings) -> bool:
    saved = (state.get("completed") or {}).get(stage) or {}
    return saved.get("binding") == bindings.get(stage) and _outputs_ok(run_dir, stage)


def _outputs_ok(run_dir: Path, stage: str) -> bool:
    need = {
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
    return all((run_dir / name).is_file() and (run_dir / name).stat().st_size > 0 for name in need)


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
        "jev": _jev_disabled(),
        "timing": state.get("timing"),
    }
    return payload


def _jev_disabled() -> dict:
    return {"enabled": False, "reason": "runner_forces_off"}


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
