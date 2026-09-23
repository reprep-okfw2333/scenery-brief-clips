import json
from io import BytesIO

from PIL import Image

from scenery_brief_clips.rank import rank_run


def test_write_json_atomic_preserves_original_on_replace_failure(tmp_path, monkeypatch):
    import pytest as _pytest

    from scenery_brief_clips.store import write_json_atomic

    target = tmp_path / "ranked.json"
    target.write_text("[1]\n", encoding="utf-8")

    def boom(src, dst):
        raise OSError("replace failed")

    monkeypatch.setattr("scenery_brief_clips.store.os.replace", boom)
    with _pytest.raises(OSError):
        write_json_atomic(target, [2])

    assert target.read_text(encoding="utf-8") == "[1]\n"
    assert list(tmp_path.glob("*.tmp")) == []


def _jpeg(color: tuple[int, int, int]) -> bytes:
    img = Image.new("RGB", (10, 10), color)
    buf = BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_rank_run_writes_ranked_json(tmp_path):
    run_dir = tmp_path / "run"
    cache = tmp_path / "meta"
    run_dir.mkdir()
    cache.mkdir()
    (run_dir / "candidates.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "abcdefghijk",
                    "title": "Alps",
                    "width": 1920,
                    "height": 1080,
                    "duration_s": 40,
                    "duration_hint": "short",
                }
            ]
        )
    )
    (cache / "abcdefghijk.json").write_text(
        json.dumps(
            {
                "id": "abcdefghijk",
                "duration": 40,
                "formats": [
                    {
                        "format_id": "sb1",
                        "format_note": "storyboard",
                        "protocol": "mhtml",
                        "width": 10,
                        "height": 10,
                        "rows": 1,
                        "columns": 1,
                        "fps": 0.1,
                        "fragments": [
                            {"url": "http://example.test/M0.jpg", "duration": 10},
                            {"url": "http://example.test/M1.jpg", "duration": 10},
                            {"url": "http://example.test/M2.jpg", "duration": 10},
                            {"url": "http://example.test/M3.jpg", "duration": 10},
                        ],
                    }
                ],
            }
        )
    )
    rows = rank_run(
        run_dir,
        cache,
        fetcher=lambda url: _jpeg((40, 160, 50)),
        max_videos=5,
        max_tiles=4,
    )
    assert (run_dir / "ranked.json").is_file()
    assert rows[0]["priority"] == "uncertain"
    assert rows[0]["windows"]
    saved = json.loads((run_dir / "ranked.json").read_text())
    assert saved[0]["video_id"] == "abcdefghijk"


def test_rank_run_missing_metadata_is_unknown(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "candidates.json").write_text(
        json.dumps([{"video_id": "missingxxxx", "title": "Gone"}])
    )
    rows = rank_run(run_dir, tmp_path / "empty-cache", fetcher=lambda url: b"", max_videos=1)
    assert rows[0]["priority"] == "unknown"
    assert rows[0]["reason"] == "missing_metadata"
