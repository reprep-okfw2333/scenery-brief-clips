from __future__ import annotations

import json
import math
from pathlib import Path

from scenery_brief_clips.run_lock import exclusive_run_lock
from scenery_brief_clips.store import write_json_atomic
from scenery_brief_clips.storyboard import RankResult

SCORE_TIME_TOLERANCE_S = 0.5
VALID_LABELS = {"keep", "reject", "uncertain"}


def _validate_scores_payload(payload) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("vision scores must be a JSON object keyed by video_id")
    for video_id, entries in payload.items():
        if not isinstance(entries, list):
            raise ValueError(f"vision scores for {video_id} must be a list")
        for score in entries:
            if not isinstance(score, dict):
                raise ValueError(f"vision score entries for {video_id} must be objects")
            label = score.get("label")
            if label is not None and str(label) not in VALID_LABELS:
                raise ValueError(
                    f"unknown vision label {label!r} for {video_id}; "
                    f"expected one of {sorted(VALID_LABELS)}"
                )
            if "t_s" in score and score["t_s"] is not None:
                try:
                    t_s = float(score["t_s"])
                except (TypeError, ValueError) as exc:
                    raise ValueError(f"vision score t_s for {video_id} must be a number") from exc
                if not math.isfinite(t_s):
                    raise ValueError(f"vision score t_s for {video_id} must be finite")
            path = score.get("path")
            if path is not None and not isinstance(path, str):
                raise ValueError(f"vision score path for {video_id} must be a string")
    return payload


def _matching_score(tile: dict, scores: list[dict]) -> dict | None:
    tile_path = tile.get("path")
    if tile_path:
        for score in scores:
            if score.get("path") and str(score["path"]) == str(tile_path):
                return score

    if "t_s" not in tile:
        return None
    t_s = float(tile["t_s"])
    timed = [score for score in scores if "t_s" in score and not score.get("path")]
    if not timed:
        return None
    best = min(timed, key=lambda score: abs(float(score["t_s"]) - t_s))
    if abs(float(best["t_s"]) - t_s) > SCORE_TIME_TOLERANCE_S:
        return None
    return best


def _windows_for_times(times: list[float], interval_s: float) -> list[dict]:
    """Make windows only from adjacent selected tiles.

    A missing/rejected tile creates a gap; it is never bridged merely because two
    kept tiles are within two storyboard intervals.
    """
    if not times:
        return []
    interval_s = max(float(interval_s), 0.001)
    windows: list[dict] = []
    for start in sorted(set(times)):
        end = start + interval_s
        if windows and start <= float(windows[-1]["end_s"]) + 1e-6:
            windows[-1]["end_s"] = max(float(windows[-1]["end_s"]), end)
        else:
            windows.append({"start_s": start, "end_s": end})
    return windows


def apply_tile_scores(
    tiles: list[dict],
    scores: list[dict],
    interval_s: float,
) -> RankResult:
    if not tiles:
        return RankResult(priority="unknown", reason="no_storyboard", tiles=[])

    if not scores:
        if not any(tile.get("ok") for tile in tiles):
            return RankResult(priority="unknown", reason="fetch_failed", tiles=tiles)
        live = [float(t["t_s"]) for t in tiles if t.get("ok") and not t.get("dark")]
        if not live:
            return RankResult(priority="low", reason="dark_or_empty", tiles=tiles)
        return RankResult(
            priority="uncertain",
            reason="no_vision_backend",
            windows=_windows_for_times(live, interval_s),
            tiles=tiles,
        )

    keep_times: list[float] = []
    uncertain_times: list[float] = []
    saw_keep = False
    saw_uncertain = False
    saw_reject = False
    annotated: list[dict] = []
    for tile in tiles:
        row = dict(tile)
        if not tile.get("ok") or tile.get("dark"):
            annotated.append(row)
            continue
        t_s = float(tile["t_s"])
        score = _matching_score(tile, scores)
        if score is None:
            row["vision_label"] = "uncertain"
            uncertain_times.append(t_s)
            saw_uncertain = True
        else:
            label = str(score.get("label") or "uncertain")
            row["vision_label"] = label
            row["vision_look"] = score.get("look")
            row["vision_note"] = score.get("note")
            if label == "keep":
                keep_times.append(t_s)
                saw_keep = True
            elif label == "reject":
                saw_reject = True
            else:
                uncertain_times.append(t_s)
                saw_uncertain = True
        annotated.append(row)

    if saw_keep:
        return RankResult(
            priority="promising",
            reason="vision_keep",
            windows=_windows_for_times(keep_times, interval_s),
            tiles=annotated,
        )
    if saw_uncertain:
        return RankResult(
            priority="uncertain",
            reason="vision_uncertain",
            windows=_windows_for_times(uncertain_times, interval_s),
            tiles=annotated,
        )
    if saw_reject:
        return RankResult(priority="low", reason="vision_reject", tiles=annotated)
    return RankResult(priority="unknown", reason="no_storyboard", tiles=annotated)


def apply_scores_run(run_dir: str | Path, scores_path: str | Path) -> list[dict]:
    rows, _unmatched = apply_scores_run_detailed(run_dir, scores_path)
    return rows


def apply_scores_run_detailed(
    run_dir: str | Path, scores_path: str | Path
) -> tuple[list[dict], list[str]]:
    with exclusive_run_lock(run_dir):
        return _apply_scores_run_locked(run_dir, scores_path)


def _apply_scores_run_locked(run_dir: str | Path, scores_path: str | Path) -> list[dict]:
    run_dir = Path(run_dir)
    ranked_path = run_dir / "ranked.json"
    scores_all = _validate_scores_payload(
        json.loads(Path(scores_path).read_text(encoding="utf-8"))
    )
    ranked = json.loads(ranked_path.read_text(encoding="utf-8"))
    ranked_ids = {str(row.get("video_id") or "") for row in ranked}
    unmatched = sorted(set(scores_all) - ranked_ids)
    backup = run_dir / "ranked_before_vision.json"
    if not backup.is_file():
        write_json_atomic(backup, ranked)
    out: list[dict] = []
    for row in ranked:
        video_id = str(row.get("video_id") or "")
        tiles = row.get("tiles") or []
        if not tiles and row.get("priority") == "unknown":
            out.append(dict(row))
            continue
        interval = float(row.get("interval_s") or 10.0)
        result = apply_tile_scores(tiles, scores_all.get(video_id) or [], interval)
        updated = dict(row)
        updated["priority"] = result.priority
        updated["reason"] = result.reason
        updated["windows"] = result.windows
        updated["tiles"] = result.tiles
        out.append(updated)
    write_json_atomic(ranked_path, out)
    return out, unmatched
