from io import BytesIO

from PIL import Image

from scenery_brief_clips.storyboard import (
    choose_storyboard,
    list_storyboards,
    rank_storyboard,
    sample_tiles,
    slice_tile,
    tile_time_s,
)


def _fmt(*, fid, w, h, rows, cols, fps, nfrag, duration_each=None):
    tiles = rows * cols
    dur = duration_each if duration_each is not None else (tiles / fps if fps else 1.0)
    return {
        "format_id": fid,
        "format_note": "storyboard",
        "protocol": "mhtml",
        "width": w,
        "height": h,
        "rows": rows,
        "columns": cols,
        "fps": fps,
        "fragments": [{"url": f"http://example.test/{fid}/M{i}.jpg", "duration": dur} for i in range(nfrag)],
    }


def test_list_storyboards_ignores_video_formats():
    formats = [
        {"format_id": "137", "width": 1920, "height": 1080, "vcodec": "avc1", "acodec": "none"},
        _fmt(fid="sb1", w=160, h=90, rows=5, cols=5, fps=0.1, nfrag=4),
    ]
    boards = list_storyboards(formats)
    assert len(boards) == 1
    assert boards[0]["format_id"] == "sb1"


def test_choose_prefers_larger_tiles_with_usable_fps():
    sparse = _fmt(fid="sb3", w=48, h=27, rows=10, cols=10, fps=0.002, nfrag=1)
    mid = _fmt(fid="sb1", w=160, h=90, rows=5, cols=5, fps=0.1, nfrag=20)
    dense = _fmt(fid="sb0", w=320, h=180, rows=3, cols=3, fps=0.1, nfrag=40)
    chosen = choose_storyboard([sparse, mid, dense])
    assert chosen is not None
    assert chosen["format_id"] == "sb0"


def test_choose_returns_none_without_storyboards():
    assert choose_storyboard([{"format_id": "137", "height": 1080}]) is None


def test_sample_tiles_spreads_and_caps():
    board = _fmt(fid="sb1", w=160, h=90, rows=2, cols=2, fps=0.1, nfrag=5)
    refs = sample_tiles(board, max_tiles=4, duration_s=200.0)
    assert len(refs) == 4
    times = [r.t_s for r in refs]
    assert times == sorted(times)
    assert times[0] == 0.0
    assert times[-1] < 200.0
    assert len({r.fragment_index for r in refs}) >= 2


def test_sample_tiles_never_emits_time_exactly_at_duration():
    board = _fmt(fid="sb1", w=160, h=90, rows=1, cols=1, fps=0.1, nfrag=3)
    refs = sample_tiles(board, max_tiles=3, duration_s=20.0)
    assert [r.t_s for r in refs] == [0.0, 10.0]


def test_tile_time_uses_fps():
    board = _fmt(fid="sb1", w=160, h=90, rows=2, cols=2, fps=0.1, nfrag=2)
    # 2x2 = 4 tiles/sheet; index 4 is first tile of fragment 1
    assert tile_time_s(board, global_index=0) == 0.0
    assert abs(tile_time_s(board, global_index=4) - 40.0) < 1e-6


def test_slice_tile_from_grid():
    img = Image.new("RGB", (40, 20), (0, 0, 0))
    # 2x2 tiles of 20x10: top-right red
    for x in range(20, 40):
        for y in range(0, 10):
            img.putpixel((x, y), (255, 0, 0))
    tile = slice_tile(img, rows=2, columns=2, row=0, col=1, tile_w=20, tile_h=10)
    assert tile.size == (20, 10)
    assert tile.getpixel((0, 0)) == (255, 0, 0)


def test_rank_missing_storyboard_is_unknown():
    result = rank_storyboard(formats=[], duration_s=100.0, max_tiles=8, fetcher=lambda url: b"")
    assert result.priority == "unknown"
    assert result.windows == []
    assert result.reason == "no_storyboard"


def test_rank_all_black_tiles_is_low():
    board = _fmt(fid="sb1", w=10, h=10, rows=1, cols=1, fps=0.1, nfrag=3)

    def fetcher(url: str) -> bytes:
        img = Image.new("RGB", (10, 10), (0, 0, 0))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    result = rank_storyboard(formats=[board], duration_s=30.0, max_tiles=3, fetcher=fetcher)
    assert result.priority == "low"
    assert result.reason == "dark_or_empty"


def test_rank_varied_tiles_is_uncertain_with_windows():
    board = _fmt(fid="sb1", w=10, h=10, rows=1, cols=1, fps=0.1, nfrag=4)

    def fetcher(url: str) -> bytes:
        img = Image.new("RGB", (10, 10), (30, 140, 40))
        buf = BytesIO()
        img.save(buf, format="JPEG")
        return buf.getvalue()

    result = rank_storyboard(formats=[board], duration_s=40.0, max_tiles=4, fetcher=fetcher)
    assert result.priority == "uncertain"
    assert result.windows
    assert result.windows[0]["start_s"] >= 0.0
    assert result.windows[-1]["end_s"] > result.windows[0]["start_s"]
