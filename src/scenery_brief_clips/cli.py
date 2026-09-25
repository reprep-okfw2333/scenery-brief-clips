from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
import time
from dataclasses import asdict, replace
from pathlib import Path

from scenery_brief_clips.brief import (
    BriefValidationError,
    QueryPlanValidationError,
    canonical_json_hash,
    validate_brief,
    validate_query_plan,
)
from scenery_brief_clips.config import ConfigError, load_project_config
from scenery_brief_clips.continuity import continuity_settings_from_config
from scenery_brief_clips.detect import detect_scenes
from scenery_brief_clips.fetch import cached_fetcher
from scenery_brief_clips.pipeline import run_dry
from scenery_brief_clips.pipeline_analyze import analyze_run
from scenery_brief_clips.analyze import ConstraintError
from scenery_brief_clips.planner import (
    PlannerError,
    load_planner_wire,
    plan_queries,
)
from scenery_brief_clips.prompt import parse_prompt
from scenery_brief_clips.rank import rank_run
from scenery_brief_clips.review import review_run
from scenery_brief_clips.shortlist import (
    DEFAULT_FRAMES_PER_MOMENT,
    MIN_FRAMES_PER_MOMENT,
    ShortlistInputError,
    ShortlistStaleError,
    shortlist_apply_run,
)
from scenery_brief_clips.store import MetadataCache, write_json_atomic, write_run
from scenery_brief_clips.export import ExportError, ExportStaleError, export_run
from scenery_brief_clips.models import Constraint, RunLimits
from scenery_brief_clips.verify import verify_run
from scenery_brief_clips.vision import apply_scores_run_detailed
from scenery_brief_clips.vision_wire import (
    VisionWireError,
    label_ranked_tiles,
    label_review_strips,
    load_vision_wire,
    plain_description,
)
from scenery_brief_clips.yt import YtDlp


def project_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _nonnegative_int(value: str) -> int:
    parsed = int(value)
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be at least 0")
    return parsed


def _positive_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0:
        raise argparse.ArgumentTypeError("must be a finite number greater than 0")
    return parsed


def _nonnegative_float(value: str) -> float:
    parsed = float(value)
    if not math.isfinite(parsed) or parsed < 0:
        raise argparse.ArgumentTypeError("must be a finite number at least 0")
    return parsed


def _version_line(cmd: list[str]) -> str | None:
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.TimeoutExpired):
        return None
    text = (result.stdout or result.stderr or "").strip()
    return text.splitlines()[0] if text else None


def _parse_ytdlp_version(line: str | None) -> tuple[int, ...] | None:
    if not line:
        return None
    # yt-dlp prints bare dates like 2024.08.06 or 2026.08.19
    token = line.strip().split()[0]
    parts = token.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        return None
    return tuple(int(part) for part in parts)


# yt-dlp's EJS/runtime-based YouTube support is required by our --js-runtimes
# acquisition calls. The external runtime transition shipped in 2025.11.12.
_MIN_YTDLP_VERSION = (2025, 11, 12)


def doctor(root: Path) -> dict:
    tmp_dir = root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    data_dir = root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    yt_dlp = shutil.which("yt-dlp")
    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    node = shutil.which("node")
    pillow = True
    try:
        import PIL  # noqa: F401
    except ImportError:
        pillow = False
    scenedetect = True
    try:
        import scenedetect as _sd  # noqa: F401
    except ImportError:
        scenedetect = False

    python_version = (
        f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}"
    )
    yt_dlp_version = _version_line([yt_dlp, "--version"]) if yt_dlp else None
    ffmpeg_version = _version_line([ffmpeg, "-version"]) if ffmpeg else None
    ffprobe_version = _version_line([ffprobe, "-version"]) if ffprobe else None
    node_version = _version_line([node, "--version"]) if node else None

    messages: list[str] = []
    parsed_yt = _parse_ytdlp_version(yt_dlp_version)
    if yt_dlp is None:
        messages.append("yt-dlp not found on PATH; install yt-dlp and re-run doctor")
    elif parsed_yt is None:
        messages.append(
            "could not parse yt-dlp --version; install a current yt-dlp "
            f"(>={'.'.join(str(p) for p in _MIN_YTDLP_VERSION)})"
        )
    elif parsed_yt < _MIN_YTDLP_VERSION:
        messages.append(
            f"yt-dlp {yt_dlp_version} is too old for modern YouTube JS challenges; "
            f"upgrade to >={'.'.join(str(p) for p in _MIN_YTDLP_VERSION)} "
            "(pip install -U yt-dlp)"
        )
    if node is None:
        messages.append(
            "node not found on PATH; yt-dlp --js-runtimes node needs Node.js for YouTube extraction"
        )
    if ffmpeg is None or ffprobe is None:
        messages.append("ffmpeg/ffprobe required for analysis, review, and export")
    if not pillow:
        messages.append("Pillow missing from the project venv (pip/uv install pillow)")
    if not scenedetect:
        messages.append("scenedetect missing from the project venv")

    ok = bool(
        yt_dlp
        and ffmpeg
        and ffprobe
        and node
        and pillow
        and scenedetect
        and (parsed_yt is None or parsed_yt >= _MIN_YTDLP_VERSION)
    )
    # Presence without a parseable version still reports ok=False via messages,
    # but keep ok True only when binaries + deps exist and yt-dlp is new enough
    # when the version is known.
    if yt_dlp and parsed_yt is not None and parsed_yt < _MIN_YTDLP_VERSION:
        ok = False

    return {
        "ok": ok,
        "yt_dlp": yt_dlp,
        "ffmpeg": ffmpeg,
        "ffprobe": ffprobe,
        "node": node,
        "pillow": pillow,
        "scenedetect": scenedetect,
        "versions": {
            "python": python_version,
            "yt_dlp": yt_dlp_version,
            "ffmpeg": ffmpeg_version,
            "ffprobe": ffprobe_version,
            "node": node_version,
        },
        "messages": messages,
        "tmp_dir": str(tmp_dir.resolve()),
        "data_dir": str(data_dir.resolve()),
        "allow_download": False,
        "analysis_copy": "720p_video_only_capped",
        "export_policy": "sections-only shortlist export at the configured cap (default 720p via export_max_height); requires allow_export or --allow-export",
        "docs": str((root / "docs").resolve()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="scenery-brief-clips")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("doctor", help="Check local binaries and directories")

    p_explain = sub.add_parser("explain-prompt", help="Compile a prompt to JSON")
    p_explain.add_argument("prompt")

    p_run = sub.add_parser("run", help="Search and filter candidates (metadata only)")
    p_run.add_argument("--prompt", required=True)
    p_run.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="Required in Part 1. Never downloads video.",
    )
    p_run.add_argument("--max-results", type=_positive_int, default=None)
    p_run.add_argument("--max-metadata", type=_nonnegative_int, default=None)
    p_run.add_argument("--sleep", type=_nonnegative_float, default=None)
    p_run.add_argument(
        "--root",
        type=Path,
        default=None,
        help="Project root (default: install location)",
    )
    p_run.add_argument("--config", type=Path, default=None)

    p_rb = sub.add_parser(
        "run-brief",
        help="Frozen-brief discovery: one planner call or a frozen plan, then metadata-only search",
    )
    p_rb.add_argument("--brief", type=Path, required=True)
    p_rb.add_argument(
        "--dry-run",
        action="store_true",
        required=True,
        help="Required. Never downloads video; metadata eligibility only.",
    )
    p_rb.add_argument(
        "--plan",
        type=Path,
        default=None,
        help="Use this frozen plan JSON instead of calling the planner model",
    )
    p_rb.add_argument("--planner-config", type=Path, default=None)
    p_rb.add_argument("--max-results", type=_positive_int, default=None)
    p_rb.add_argument("--max-metadata", type=_nonnegative_int, default=None)
    p_rb.add_argument("--sleep", type=_nonnegative_float, default=None)
    p_rb.add_argument("--root", type=Path, default=None)
    p_rb.add_argument("--config", type=Path, default=None)

    p_rank = sub.add_parser("rank", help="Storyboard sample + cheap rank for a run")
    p_rank.add_argument("--run-dir", type=Path, required=True)
    p_rank.add_argument("--max-videos", type=_positive_int, default=None)
    p_rank.add_argument("--max-tiles", type=_positive_int, default=None)
    p_rank.add_argument("--root", type=Path, default=None)
    p_rank.add_argument("--config", type=Path, default=None)

    p_jg = sub.add_parser(
        "jev-gate",
        help="Optional Jev metadata gate before rank/analyze (needs jev_gate: true in config; key from OPENROUTER_API_KEY)",
    )
    p_jg.add_argument("--run-dir", type=Path, required=True)
    p_jg.add_argument("--root", type=Path, default=None)
    p_jg.add_argument("--config", type=Path, default=None)

    p_an = sub.add_parser("analyze", help="720p analysis copy + scene cuts + excerpts (parallel spans by default)")
    p_an.add_argument("--run-dir", type=Path, required=True)
    p_an.add_argument("--max-videos", type=_positive_int, default=None)
    p_an.add_argument("--max-analysis-s", type=_positive_float, default=None)
    p_an.add_argument("--root", type=Path, default=None)
    p_an.add_argument("--config", type=Path, default=None)

    p_ver = sub.add_parser("verify", help="Check a run dir: candidates ⊆ ranked ⊆ excerpts")
    p_ver.add_argument("--run-dir", type=Path, required=True)
    p_ver.add_argument("--root", type=Path, default=None)
    p_ver.add_argument(
        "--require-export", action="store_true", help="Fail when the run has no published export"
    )

    p_sc = sub.add_parser("apply-scores", help="Apply vision tile scores to ranked.json")
    p_sc.add_argument("--run-dir", type=Path, required=True)
    p_sc.add_argument("--scores", type=Path, required=True)
    p_sc.add_argument("--root", type=Path, default=None)

    p_sr = sub.add_parser(
        "shortlist-review", help="Extract review frames + strips for candidate moments"
    )
    p_sr.add_argument("--run-dir", type=Path, required=True)
    p_sr.add_argument("--frames", type=_positive_int, default=None)
    p_sr.add_argument("--root", type=Path, default=None)

    p_sa = sub.add_parser(
        "shortlist-apply", help="Apply shortlist labels and write shortlist.json"
    )
    p_sa.add_argument("--run-dir", type=Path, required=True)
    p_sa.add_argument("--scores", type=Path, required=True)
    p_sa.add_argument("--root", type=Path, default=None)

    p_ex = sub.add_parser(
        "export", help="Export shortlisted moments as final clips (needs explicit authorization)"
    )
    p_ex.add_argument("--run-dir", type=Path, required=True)
    p_ex.add_argument("--theme", default=None)
    p_ex.add_argument("--allow-export", action="store_true")
    p_ex.add_argument("--root", type=Path, default=None)
    p_ex.add_argument("--config", type=Path, default=None)

    p_vs = sub.add_parser("vision-show", help="Show which model is wired to look at pictures")
    p_vs.add_argument("--root", type=Path, default=None)

    p_lt = sub.add_parser("label-tiles", help="Label storyboard tiles with the wired vision model")
    p_lt.add_argument("--run-dir", type=Path, required=True)
    p_lt.add_argument("--confirm-vision", action="store_true")
    p_lt.add_argument("--root", type=Path, default=None)

    p_ls = sub.add_parser("label-strips", help="Label review strips with the wired vision model")
    p_ls.add_argument("--run-dir", type=Path, required=True)
    p_ls.add_argument("--confirm-vision", action="store_true")
    p_ls.add_argument("--root", type=Path, default=None)

    args = parser.parse_args(argv)
    if args.command == "doctor":
        report = doctor(project_root())
        print(json.dumps(report, indent=2))
        return 0 if report["ok"] else 1
    if args.command == "explain-prompt":
        print(json.dumps(asdict(parse_prompt(args.prompt)), indent=2))
        return 0
    if args.command == "run":
        return _cmd_run(args)
    if args.command == "run-brief":
        return _cmd_run_brief(args)
    if args.command == "rank":
        return _cmd_rank(args)
    if args.command == "analyze":
        return _cmd_analyze(args)
    if args.command == "jev-gate":
        return _cmd_jev_gate(args)
    if args.command == "verify":
        return _cmd_verify(args)
    if args.command == "apply-scores":
        return _cmd_apply_scores(args)
    if args.command == "shortlist-review":
        return _cmd_shortlist_review(args)
    if args.command == "shortlist-apply":
        return _cmd_shortlist_apply(args)
    if args.command == "export":
        return _cmd_export(args)
    if args.command == "vision-show":
        root = Path(args.root) if args.root else project_root()
        return _cmd_vision_show(root)
    if args.command == "label-tiles":
        return _cmd_label_tiles(args)
    if args.command == "label-strips":
        return _cmd_label_strips(args)
    parser.error(f"unknown command {args.command}")
    return 2


def _cmd_run(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    try:
        config = load_project_config(root, args.config)
    except ConfigError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    tmp_dir = root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    constraint = parse_prompt(args.prompt)
    config_aspect_min = float(config.get("aspect_min", constraint.aspect_min))
    config_aspect_max = float(config.get("aspect_max", constraint.aspect_max))
    aspect_min = max(constraint.aspect_min, config_aspect_min)
    aspect_max = min(constraint.aspect_max, config_aspect_max)
    if aspect_min > aspect_max:
        print("invalid config: configured aspect band does not overlap the prompt", file=sys.stderr)
        return 2
    constraint = replace(
        constraint,
        min_width=max(constraint.min_width, int(config.get("min_width", constraint.min_width))),
        min_height=max(constraint.min_height, int(config.get("min_height", constraint.min_height))),
        aspect_min=aspect_min,
        aspect_max=aspect_max,
        allow_download=False,
    )
    limits = constraint.limits
    max_results = (
        args.max_results
        if args.max_results is not None
        else config.get("max_search_results")
    )
    max_metadata = (
        args.max_metadata
        if args.max_metadata is not None
        else config.get("max_metadata_fetches")
    )
    sleep_s = args.sleep if args.sleep is not None else config.get("sleep_s")
    if max_results is not None:
        limits = replace(limits, max_search_results=int(max_results))
    if max_metadata is not None:
        limits = replace(limits, max_metadata_fetches=int(max_metadata))
    if sleep_s is not None:
        limits = replace(limits, sleep_s=float(sleep_s))
    constraint = replace(constraint, allow_download=False, limits=limits)

    yt = YtDlp(tmp_dir=tmp_dir, allow_download=False)
    cache = MetadataCache(root / "data" / "cache" / "metadata")
    result = run_dry(constraint, yt=yt, cache=cache, sleep_fn=time.sleep)
    log_text = "\n".join(result.log_lines) + ("\n" if result.log_lines else "")
    run_dir = write_run(
        root / "data" / "runs",
        constraint=constraint,
        candidates=result.candidates,
        rejected=result.rejected,
        log_text=log_text,
    )
    summary = {
        "run_dir": str(run_dir),
        "queries": result.queries,
        "candidates": len(result.candidates),
        "rejected": len(result.rejected),
        "stopped_reason": result.stopped_reason,
        "allow_download": False,
    }
    print(json.dumps(summary, indent=2))
    return 1 if result.stopped_reason in ("search_error", "metadata_errors") else 0


def _brief_constraint_from_brief(brief: dict, limits_overrides: dict) -> tuple[Constraint, RunLimits]:
    """Build (Constraint, RunLimits) from a validated brief the way the lab benchmark did."""
    geometry = brief["source_geometry"]
    durations = brief["clip_duration_s"]
    limits = RunLimits(
        max_search_results=int(brief["search_limits"]["max_search_results"]),
        max_metadata_fetches=int(brief["search_limits"]["max_metadata_fetches"]),
        max_bytes=0,
        max_seconds=120,
        sleep_s=float(brief["search_limits"]["sleep_s"]),
    )
    max_results = limits_overrides.get("max_results")
    if max_results is not None:
        limits = replace(limits, max_search_results=min(limits.max_search_results, int(max_results)))
    max_metadata = limits_overrides.get("max_metadata")
    if max_metadata is not None:
        limits = replace(limits, max_metadata_fetches=min(limits.max_metadata_fetches, int(max_metadata)))
    sleep_s = limits_overrides.get("sleep")
    if sleep_s is not None:
        limits = replace(limits, sleep_s=min(limits.sleep_s, float(sleep_s)))
    constraint = Constraint(
        theme_text=brief["theme_text"],
        min_width=int(geometry["min_width"]),
        min_height=int(geometry["min_height"]),
        aspect_min=float(geometry["aspect_min"]),
        aspect_max=float(geometry["aspect_max"]),
        n_clips=int(brief["n_clips"]),
        target_duration_s=float(durations["target"]),
        duration_min_s=float(durations["min"]),
        duration_max_s=float(durations["max"]),
        geo_requirement="european" if brief["geography"] == "european" else "none",
        allow_download=False,
        limits=limits,
    )
    return constraint, limits


def _load_json_file(path: Path, label: str):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        print(f"cannot read {label}: {exc}", file=sys.stderr)
        return None
    except json.JSONDecodeError as exc:
        print(f"invalid {label} JSON: {exc}", file=sys.stderr)
        return None


def _cmd_run_brief(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    brief_path = Path(args.brief)
    if not brief_path.is_file():
        print(f"brief file not found: {brief_path}", file=sys.stderr)
        return 2
    brief = _load_json_file(brief_path, "brief")
    if brief is None:
        return 2
    try:
        validate_brief(brief)
    except BriefValidationError as exc:
        print(f"invalid brief ({exc.code}): {exc}", file=sys.stderr)
        return 2
    brief_sha256 = canonical_json_hash(brief)

    plan = None
    plan_provenance = None
    if args.plan is not None:
        plan_path = Path(args.plan)
        if not plan_path.is_file():
            print(f"plan file not found: {plan_path}", file=sys.stderr)
            return 2
        plan = _load_json_file(plan_path, "plan")
        if plan is None:
            return 2
        try:
            validate_query_plan(plan, brief)
        except QueryPlanValidationError as exc:
            print(f"invalid query plan ({exc.code}): {exc}", file=sys.stderr)
            return 2
        plan_provenance = {
            "schema_version": "brief_plan_provenance_v1",
            "instruction_version": "search_query_planner_v1",
            "plan_sha256": canonical_json_hash(plan),
            "source": "frozen_plan_file",
        }
    else:
        try:
            wire = load_planner_wire(root, args.planner_config)
        except PlannerError as exc:
            print(str(exc), file=sys.stderr)
            return 2
        try:
            plan, plan_provenance = plan_queries(brief, wire)
        except PlannerError as exc:
            print(f"planner failed: {exc}", file=sys.stderr)
            return 2
        print(json.dumps({"plan_provenance": plan_provenance}, indent=2))

    queries = [item["query"] for item in plan["queries"]]
    if not queries:
        print(json.dumps({"stopped_reason": "empty_query_plan", "brief_sha256": brief_sha256}, indent=2))
        return 1

    constraint, limits = _brief_constraint_from_brief(
        brief,
        {
            "max_results": args.max_results,
            "max_metadata": args.max_metadata,
            "sleep": args.sleep,
        },
    )
    tmp_dir = root / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    yt = YtDlp(tmp_dir=tmp_dir, allow_download=False)
    cache = MetadataCache(root / "data" / "cache" / "metadata")
    result = run_dry(constraint, yt=yt, cache=cache, sleep_fn=time.sleep, queries=queries)
    log_text = "\n".join(result.log_lines) + ("\n" if result.log_lines else "")
    run_dir = write_run(
        root / "data" / "runs",
        constraint=constraint,
        candidates=result.candidates,
        rejected=result.rejected,
        log_text=log_text,
    )
    candidates_json = json.loads((run_dir / "candidates.json").read_text(encoding="utf-8"))
    rejected_json = json.loads((run_dir / "rejected.json").read_text(encoding="utf-8"))
    for record in candidates_json + rejected_json:
        record["visual_status"] = "unverified"
        record["acceptance_level"] = "metadata_only"
    discovery = {
        "schema_version": "brief_discovery_v1",
        "brief_sha256": brief_sha256,
        "query_plan": plan,
        "plan_provenance": plan_provenance,
        "attempted_queries": result.queries,
        "counts": {
            "attempted_queries": len(result.queries),
            "distinct_ids": len({c.video_id for c in result.candidates} | {r.video_id for r in result.rejected}),
            "candidates": len(result.candidates),
            "rejected": len(result.rejected),
        },
        "stopped_reason": result.stopped_reason,
        "allow_download": False,
        "candidates": candidates_json,
        "rejected": rejected_json,
    }
    write_json_atomic(run_dir / "discovery.json", discovery)
    # Overwrite the run files with the annotated records so the run dir is
    # self-describing; the discovery sidecar keeps the plan provenance.
    write_json_atomic(run_dir / "candidates.json", candidates_json)
    write_json_atomic(run_dir / "rejected.json", rejected_json)
    summary = {
        "run_dir": str(run_dir),
        "brief_sha256": brief_sha256,
        "queries": result.queries,
        "candidates": len(result.candidates),
        "rejected": len(result.rejected),
        "stopped_reason": result.stopped_reason,
        "allow_download": False,
        "visual_status": "unverified",
        "acceptance_level": "metadata_only",
    }
    print(json.dumps(summary, indent=2))
    return 1 if result.stopped_reason in ("search_error", "metadata_errors") else 0


def _require_run_dir(run_dir: Path) -> int | None:
    if not run_dir.is_dir():
        print(f"run dir not found: {run_dir}", file=sys.stderr)
        return 2
    return None


def _cmd_rank(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    try:
        config = load_project_config(root, args.config)
    except ConfigError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    max_videos = (
        args.max_videos
        if args.max_videos is not None
        else int(config.get("max_rank_videos", 10))
    )
    max_tiles = (
        args.max_tiles if args.max_tiles is not None else int(config.get("max_tiles", 12))
    )
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (root / run_dir).resolve()
    else:
        run_dir = run_dir.resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    if not (run_dir / "candidates.json").is_file():
        print(f"missing candidates.json in {run_dir}", file=sys.stderr)
        return 2
    rows = rank_run(
        run_dir,
        root / "data" / "cache" / "metadata",
        fetcher=cached_fetcher(root / "data" / "cache" / "storyboards"),
        max_videos=max_videos,
        max_tiles=max_tiles,
        tiles_root=root / "data" / "cache" / "tiles",
    )
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("priority"))
        counts[key] = counts.get(key, 0) + 1
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "ranked": len(rows),
                "by_priority": counts,
                "ranked_path": str(run_dir / "ranked.json"),
                "allow_download": False,
            },
            indent=2,
        )
    )
    return 0


def _cmd_jev_gate(args: argparse.Namespace) -> int:
    from scenery_brief_clips.jev_gate import JevGateSettings, gate_run

    root = Path(args.root) if args.root else project_root()
    try:
        config = load_project_config(root, args.config)
    except ConfigError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    run_dir = Path(args.run_dir)
    run_dir = (root / run_dir).resolve() if not run_dir.is_absolute() else run_dir.resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    if not (run_dir / "candidates.json").is_file():
        print(f"missing candidates.json in {run_dir}", file=sys.stderr)
        return 2
    settings = JevGateSettings.from_config(config)
    result = gate_run(
        run_dir,
        metadata_cache=root / "data" / "cache" / "metadata",
        jev_cache=root / "data" / "cache" / "jev",
        settings=settings,
    )
    if not result.get("enabled"):
        result["note"] = "jev_gate is off (set jev_gate: true in config); candidates.json unchanged"
    print(json.dumps(result, indent=2))
    return 0


def _cmd_analyze(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    try:
        config = load_project_config(root, args.config)
    except ConfigError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    max_videos = (
        args.max_videos
        if args.max_videos is not None
        else int(config.get("max_analyze_videos", 1))
    )
    max_analysis_s = (
        args.max_analysis_s
        if args.max_analysis_s is not None
        else float(config.get("max_analysis_s", 120.0))
    )
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (root / run_dir).resolve()
    else:
        run_dir = run_dir.resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    if not (run_dir / "ranked.json").is_file():
        print(f"missing ranked.json in {run_dir}", file=sys.stderr)
        return 2
    yt = YtDlp(tmp_dir=root / "tmp", allow_download=False)

    def fetch_span(video_id, dest, span):
        return yt.fetch_analysis(video_id, dest, span, timeout=300)

    try:
        continuity = continuity_settings_from_config(config)
    except ConstraintError as exc:
        print(f"invalid continuity settings in config: {exc}", file=sys.stderr)
        return 2
    try:
        rows = analyze_run(
            run_dir,
            cache_dir=root / "data" / "cache" / "analysis",
            fetch_span=fetch_span,
            detect_fn=detect_scenes,
            max_videos=max_videos,
            max_analysis_s=max_analysis_s,
            invalidate_span=yt.invalidate_analysis,
            continuity_settings=continuity,
        )
    except ConstraintError as exc:
        print(f"invalid duration settings in constraint: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(
            json.dumps(
                {
                    "run_dir": str(run_dir),
                    "status": "failed",
                    "error": str(exc),
                    "allow_download": False,
                    "analysis_copy": "720p_video_only_capped",
                },
                indent=2,
            )
        )
        return 1
    n_excerpts = sum(len(row.get("excerpts") or []) for row in rows)
    n_skipped = sum(1 for row in rows if row.get("status") == "skipped")
    by_status: dict[str, int] = {}
    valid_statuses = {"complete", "partial", "failed", "skipped"}
    for row in rows:
        raw_status = row.get("status")
        status = str(raw_status) if raw_status in valid_statuses else "invalid"
        by_status[status] = by_status.get(status, 0) + 1
    range_errors = sum(len(row.get("errors") or []) for row in rows)
    recovered_attempt_errors = sum(
        len(outcome.get("attempt_errors") or [])
        for row in rows
        for outcome in row.get("ranges") or []
        if outcome.get("status") == "complete"
    )
    videos_without_excerpts = sum(
        1
        for row in rows
        if row.get("status") == "complete" and not (row.get("excerpts") or [])
    )
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "videos": len(rows),
                "skipped": n_skipped,
                "by_status": by_status,
                "range_errors": range_errors,
                "recovered_attempt_errors": recovered_attempt_errors,
                "videos_without_excerpts": videos_without_excerpts,
                "excerpts": n_excerpts,
                "excerpts_path": str(run_dir / "excerpts.json"),
                "manifest_path": str(run_dir / "analysis_manifest.json"),
                "allow_download": False,
                "analysis_copy": "720p_video_only_capped",
            },
            indent=2,
        )
    )
    failed = bool(
        range_errors
        or by_status.get("partial", 0)
        or by_status.get("failed", 0)
        or by_status.get("invalid", 0)
    )
    return 1 if failed else 0


def _cmd_verify(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (root / run_dir).resolve()
    else:
        run_dir = run_dir.resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    report = verify_run(
        run_dir,
        analysis_dir=root / "data" / "cache" / "analysis",
        root=root,
        require_export=bool(getattr(args, "require_export", False)),
    )
    print(json.dumps(report, indent=2))
    return 0 if report["ok"] else 1


def _cmd_export(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (root / run_dir).resolve()
    else:
        run_dir = run_dir.resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    try:
        config = load_project_config(root, args.config)
    except ConfigError as exc:
        print(f"invalid config: {exc}", file=sys.stderr)
        return 2
    try:
        manifest = export_run(
            run_dir,
            root,
            theme=args.theme,
            allow_export=bool(args.allow_export),
            config=config,
        )
    except ExportStaleError as exc:
        print(f"export inputs are stale: {exc}", file=sys.stderr)
        return 1
    except ExportError as exc:
        print(f"cannot export: {exc}", file=sys.stderr)
        return 2
    counts = manifest.get("counts") or {}
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "theme": manifest.get("theme"),
                "counts": counts,
                "failed": manifest.get("failed") or [],
            },
            indent=2,
        )
    )
    return 1 if counts.get("failed") else 0


def _cmd_apply_scores(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (root / run_dir).resolve()
    else:
        run_dir = run_dir.resolve()
    scores = Path(args.scores)
    if not scores.is_absolute():
        scores = (root / scores).resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    if not (run_dir / "ranked.json").is_file():
        print(f"missing ranked.json in {run_dir}", file=sys.stderr)
        return 2
    if not scores.is_file():
        print(f"missing scores file {scores}", file=sys.stderr)
        return 2
    try:
        rows, unmatched = apply_scores_run_detailed(run_dir, scores)
    except ValueError as exc:
        print(f"invalid vision scores: {exc}", file=sys.stderr)
        return 2
    if unmatched:
        print(
            f"warning: scores contain unmatched ids not present in ranked.json: {', '.join(unmatched)}",
            file=sys.stderr,
        )
    counts: dict[str, int] = {}
    for row in rows:
        key = str(row.get("priority"))
        counts[key] = counts.get(key, 0) + 1
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "ranked": len(rows),
                "by_priority": counts,
                "unmatched_score_ids": unmatched,
                "scores": str(scores),
            },
            indent=2,
        )
    )
    return 0


def _cmd_shortlist_review(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (root / run_dir).resolve()
    else:
        run_dir = run_dir.resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    frames = args.frames if args.frames is not None else DEFAULT_FRAMES_PER_MOMENT
    if frames < MIN_FRAMES_PER_MOMENT:
        print(f"--frames must be at least {MIN_FRAMES_PER_MOMENT}", file=sys.stderr)
        return 2
    try:
        doc = review_run(run_dir, frames_per_moment=frames)
    except ShortlistStaleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    except (ShortlistInputError, ValueError) as exc:
        print(f"invalid review request: {exc}", file=sys.stderr)
        return 2
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "moments": doc["counts"]["moments"],
                "frames": doc["counts"]["frames"],
                "errors": doc["counts"]["errors"],
                "review_path": str(run_dir / "review.json"),
            },
            indent=2,
        )
    )
    return 1 if doc["counts"]["errors"] else 0


def _cmd_shortlist_apply(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (root / run_dir).resolve()
    else:
        run_dir = run_dir.resolve()
    scores = Path(args.scores)
    if not scores.is_absolute():
        scores = (root / scores).resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    try:
        doc = shortlist_apply_run(run_dir, scores)
    except ShortlistInputError as exc:
        print(f"invalid shortlist labels: {exc}", file=sys.stderr)
        return 2
    except ShortlistStaleError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "n_clips_requested": doc["n_clips_requested"],
                "selected": doc["counts"]["n_selected"],
                "excluded": doc["counts"]["n_excluded"],
                "shortfall": doc["shortfall"]["count"],
                "shortfall_explanation": doc["shortfall"]["explanation"],
                "request_fulfilled": doc["counts"]["request_fulfilled"],
                "n_continuity_rejected_upstream": doc["counts"]["n_continuity_rejected_upstream"],
                "counts": doc["counts"],
                "shortlist_path": str(run_dir / "shortlist.json"),
            },
            indent=2,
        )
    )
    return 0


def _cmd_vision_show(root: Path) -> int:
    try:
        wire = load_vision_wire(root)
    except VisionWireError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(plain_description(wire))
    print(json.dumps(wire.as_public_dict(), indent=2))
    return 0


def _refuse_unconfirmed_vision(root: Path) -> int:
    try:
        wire = load_vision_wire(root)
        sentence = plain_description(wire)
    except VisionWireError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    print(sentence, file=sys.stderr)
    print(
        "Refusing to label pictures until this model is confirmed. "
        "Ask the user, then re-run with --confirm-vision.",
        file=sys.stderr,
    )
    return 2


def _cmd_label_tiles(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    if not args.confirm_vision:
        return _refuse_unconfirmed_vision(root)
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (root / run_dir).resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    try:
        wire = load_vision_wire(root)
        result = label_ranked_tiles(run_dir, wire)
    except VisionWireError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if result["failures"]:
        print(json.dumps({"wired": wire.as_public_dict(), "failures": result["failures"]}, indent=2))
        return 1
    write_json_atomic(run_dir / "vision_scores.json", result["scores"])
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "wired": wire.as_public_dict(),
                "scores": str(run_dir / "vision_scores.json"),
                "videos": len(result["scores"]),
            },
            indent=2,
        )
    )
    return 0


def _cmd_label_strips(args: argparse.Namespace) -> int:
    root = Path(args.root) if args.root else project_root()
    if not args.confirm_vision:
        return _refuse_unconfirmed_vision(root)
    run_dir = Path(args.run_dir)
    if not run_dir.is_absolute():
        run_dir = (root / run_dir).resolve()
    missing = _require_run_dir(run_dir)
    if missing is not None:
        return missing
    try:
        wire = load_vision_wire(root)
        result = label_review_strips(run_dir, wire)
    except VisionWireError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    if result["failures"]:
        print(json.dumps({"wired": wire.as_public_dict(), "failures": result["failures"]}, indent=2))
        return 1
    write_json_atomic(run_dir / "shortlist_scores.json", result["scores"])
    print(
        json.dumps(
            {
                "run_dir": str(run_dir),
                "wired": wire.as_public_dict(),
                "scores": str(run_dir / "shortlist_scores.json"),
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
