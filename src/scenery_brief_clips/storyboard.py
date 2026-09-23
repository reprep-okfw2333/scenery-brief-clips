from __future__ import annotations

import math
from dataclasses import dataclass, field
from io import BytesIO
from pathlib import Path
from typing import Callable

from PIL import Image

Fetcher = Callable[[str], bytes]
MIN_USABLE_FPS = 0.05
DARK_LUMA = 18.0


@dataclass(frozen=True)
class TileRef:
    global_index: int
    fragment_index: int
    row: int
    col: int
    t_s: float
    url: str


@dataclass
class RankResult:
    priority: str
    reason: str
    format_id: str | None = None
    windows: list[dict] = field(default_factory=list)
    tiles: list[dict] = field(default_factory=list)
    n_fetched_sheets: int = 0
    interval_s: float = 10.0


def list_storyboards(formats: list[dict]) -> list[dict]:
    out = []
    for fmt in formats:
        note = str(fmt.get("format_note") or "").lower()
        fid = str(fmt.get("format_id") or "")
        protocol = str(fmt.get("protocol") or "")
        if note == "storyboard" or protocol == "mhtml" or fid.startswith("sb"):
            if fmt.get("fragments"):
                out.append(fmt)
    return out


def choose_storyboard(formats: list[dict]) -> dict | None:
    boards = list_storyboards(formats)
    usable = []
    for board in boards:
        fps = float(board.get("fps") or 0.0)
        if fps >= MIN_USABLE_FPS:
            usable.append(board)
    pool = usable or boards
    if not pool:
        return None
    pool.sort(key=lambda b: (int(b.get("height") or 0), int(b.get("width") or 0)), reverse=True)
    return pool[0]


def tile_time_s(board: dict, global_index: int) -> float:
    fps = float(board.get("fps") or 0.0)
    if fps <= 0:
        return 0.0
    return global_index / fps


def _tiles_per_sheet(board: dict) -> int:
    rows = int(board.get("rows") or 1)
    cols = int(board.get("columns") or 1)
    return max(1, rows * cols)


def sample_tiles(board: dict, max_tiles: int, duration_s: float | None) -> list[TileRef]:
    fragments = board.get("fragments") or []
    if not fragments or max_tiles < 1:
        return []
    rows = int(board.get("rows") or 1)
    cols = int(board.get("columns") or 1)
    per = _tiles_per_sheet(board)
    fps = float(board.get("fps") or 0.0)
    n_from_frags = len(fragments) * per
    if duration_s and fps > 0:
        # Frames are at 0, 1/fps, ... strictly before duration_s.
        n_total = min(n_from_frags, max(1, math.ceil(duration_s * fps - 1e-9)))
    else:
        n_total = n_from_frags
    count = min(max_tiles, n_total)
    if count == 1:
        indices = [0]
    else:
        indices = []
        for i in range(count):
            idx = round(i * (n_total - 1) / (count - 1))
            if not indices or indices[-1] != idx:
                indices.append(idx)
    refs: list[TileRef] = []
    for gi in indices:
        frag_i = gi // per
        if frag_i >= len(fragments):
            continue
        rem = gi % per
        row = rem // cols
        col = rem % cols
        url = str(fragments[frag_i].get("url") or "")
        refs.append(
            TileRef(
                global_index=gi,
                fragment_index=frag_i,
                row=row,
                col=col,
                t_s=tile_time_s(board, gi),
                url=url,
            )
        )
    return refs


def slice_tile(img: Image.Image, rows: int, columns: int, row: int, col: int, tile_w: int, tile_h: int) -> Image.Image:
    x = col * tile_w
    y = row * tile_h
    return img.crop((x, y, x + tile_w, y + tile_h))


def _luma(img: Image.Image) -> float:
    rgb = img.convert("RGB")
    raw = rgb.tobytes()
    n = len(raw) // 3
    if n == 0:
        return 0.0
    acc = 0.0
    for i in range(0, len(raw), 3):
        r, g, b = raw[i], raw[i + 1], raw[i + 2]
        acc += 0.2126 * r + 0.7152 * g + 0.0722 * b
    return acc / n


def _merge_windows(times: list[float], interval_s: float) -> list[dict]:
    if not times:
        return []
    times = sorted(times)
    gap = max(interval_s * 2.0, 1.0)
    windows = []
    start = times[0]
    end = times[0] + interval_s
    for t in times[1:]:
        if t <= end + gap:
            end = t + interval_s
        else:
            windows.append({"start_s": start, "end_s": end})
            start = t
            end = t + interval_s
    windows.append({"start_s": start, "end_s": end})
    return windows


def rank_storyboard(
    formats: list[dict],
    duration_s: float | None,
    max_tiles: int,
    fetcher: Fetcher,
    tile_dir: str | Path | None = None,
) -> RankResult:
    chosen = choose_storyboard(formats)
    if chosen is None:
        return RankResult(priority="unknown", reason="no_storyboard")
    refs = sample_tiles(chosen, max_tiles=max_tiles, duration_s=duration_s)
    if not refs:
        return RankResult(priority="unknown", reason="no_storyboard", format_id=str(chosen.get("format_id")))

    sheets: dict[str, Image.Image] = {}
    fetched = 0
    tile_w = int(chosen.get("width") or 0)
    tile_h = int(chosen.get("height") or 0)
    rows = int(chosen.get("rows") or 1)
    cols = int(chosen.get("columns") or 1)
    fps = float(chosen.get("fps") or 0.0)
    interval = (1.0 / fps) if fps > 0 else 1.0

    live_times: list[float] = []
    tile_rows: list[dict] = []
    out_dir = Path(tile_dir) if tile_dir else None
    if out_dir:
        out_dir.mkdir(parents=True, exist_ok=True)
    for ref in refs:
        if ref.url not in sheets:
            try:
                raw = fetcher(ref.url)
                sheets[ref.url] = Image.open(BytesIO(raw)).convert("RGB")
                fetched += 1
            except Exception:
                tile_rows.append({"t_s": ref.t_s, "ok": False, "reason": "fetch_failed"})
                continue
        sheet = sheets[ref.url]
        tw = tile_w or max(1, sheet.width // cols)
        th = tile_h or max(1, sheet.height // rows)
        tile = slice_tile(sheet, rows=rows, columns=cols, row=ref.row, col=ref.col, tile_w=tw, tile_h=th)
        luma = _luma(tile)
        dark = luma < DARK_LUMA
        row = {"t_s": ref.t_s, "ok": True, "luma": round(luma, 2), "dark": dark}
        if out_dir:
            name = f"{ref.global_index:05d}_{int(ref.t_s):06d}.jpg"
            path = out_dir / name
            tile.save(path, format="JPEG", quality=85)
            row["path"] = str(path)
        tile_rows.append(row)
        if not dark:
            live_times.append(ref.t_s)

    if not any(t.get("ok") for t in tile_rows):
        return RankResult(
            priority="unknown",
            reason="fetch_failed",
            format_id=str(chosen.get("format_id")),
            tiles=tile_rows,
            n_fetched_sheets=fetched,
            interval_s=interval,
        )
    if not live_times:
        return RankResult(
            priority="low",
            reason="dark_or_empty",
            format_id=str(chosen.get("format_id")),
            tiles=tile_rows,
            n_fetched_sheets=fetched,
            interval_s=interval,
        )
    return RankResult(
        priority="uncertain",
        reason="no_vision_backend",
        format_id=str(chosen.get("format_id")),
        windows=_merge_windows(live_times, interval),
        tiles=tile_rows,
        n_fetched_sheets=fetched,
        interval_s=interval,
    )
