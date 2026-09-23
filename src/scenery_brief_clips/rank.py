from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

from scenery_brief_clips.run_lock import exclusive_run_lock
from scenery_brief_clips.store import write_json_atomic
from scenery_brief_clips.storyboard import RankResult, rank_storyboard


def rank_run(
    run_dir: Path,
    metadata_cache: Path,
    fetcher: Callable[[str], bytes],
    max_videos: int = 10,
    max_tiles: int = 12,
    tiles_root: Path | None = None,
) -> list[dict]:
    with exclusive_run_lock(run_dir):
        return _rank_run_locked(
            run_dir=run_dir,
            metadata_cache=metadata_cache,
            fetcher=fetcher,
            max_videos=max_videos,
            max_tiles=max_tiles,
            tiles_root=tiles_root,
        )


def _rank_run_locked(
    run_dir: Path,
    metadata_cache: Path,
    fetcher: Callable[[str], bytes],
    max_videos: int = 10,
    max_tiles: int = 12,
    tiles_root: Path | None = None,
) -> list[dict]:
    run_dir = Path(run_dir)
    candidates_path = run_dir / "candidates.json"
    candidates = json.loads(candidates_path.read_text(encoding="utf-8"))
    cache_root = Path(metadata_cache)
    ranked: list[dict] = []
    for cand in candidates[: max(0, max_videos)]:
        video_id = str(cand.get("video_id") or "")
        info_path = cache_root / f"{video_id.replace('/', '_')}.json"
        if not info_path.is_file():
            row = {
                "video_id": video_id,
                "title": cand.get("title"),
                "priority": "unknown",
                "reason": "missing_metadata",
                "windows": [],
            }
            ranked.append(row)
            continue
        info = json.loads(info_path.read_text(encoding="utf-8"))
        tile_dir = None
        if tiles_root is not None:
            tile_dir = Path(tiles_root) / video_id.replace("/", "_")
        result: RankResult = rank_storyboard(
            formats=info.get("formats") or [],
            duration_s=info.get("duration"),
            max_tiles=max_tiles,
            fetcher=fetcher,
            tile_dir=tile_dir,
        )
        ranked.append(
            {
                "video_id": video_id,
                "title": cand.get("title"),
                "priority": result.priority,
                "reason": result.reason,
                "format_id": result.format_id,
                "windows": result.windows,
                "n_fetched_sheets": result.n_fetched_sheets,
                "n_tiles": len(result.tiles),
                "interval_s": result.interval_s,
                "tiles": result.tiles,
                "width": cand.get("width"),
                "height": cand.get("height"),
                "duration_s": cand.get("duration_s"),
                "duration_hint": cand.get("duration_hint"),
            }
        )
    write_json_atomic(run_dir / "ranked.json", ranked)
    return ranked
