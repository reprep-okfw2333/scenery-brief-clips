import json
import subprocess
from pathlib import Path

import pytest

from scenery_brief_clips.analysis_cache import sha256_file
from scenery_brief_clips.review import review_run, sample_times
from scenery_brief_clips.shortlist import ShortlistInputError, ShortlistStaleError


def test_sample_times_include_endpoints_and_interior():
    # 50ms inset on a long window; 4 frames → start, 2 interior, end.
    assert sample_times(10.0, 20.0, 4) == pytest.approx([10.05, 13.35, 16.65, 19.95])
    two = sample_times(0.0, 6.0, 2)
    assert len(two) == 2
    assert two[0] == pytest.approx(0.05)
    assert two[1] == pytest.approx(5.95)
    six = sample_times(0.0, 6.0, 6)
    assert len(six) == 6
    assert six[0] == pytest.approx(0.05)
    assert six[-1] == pytest.approx(5.95)
    # Endpoints always present; interior strictly between them.
    assert six[0] < six[1] < six[-2] < six[-1]
    with pytest.raises(ShortlistInputError):
        sample_times(0.0, 6.0, 1)


def _make_source(path: Path) -> None:
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=green:s=320x180:d=4",
            "-f",
            "lavfi",
            "-i",
            "color=c=purple:s=320x180:d=4",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-pix_fmt",
            "yuv420p",
            str(path),
        ],
        check=True,
    )


def _make_run(tmp_path: Path, frames: int = 3, span_offset: float = 0.0) -> tuple[Path, str]:
    cache_dir = tmp_path / "analysis"
    cache_dir.mkdir()
    copy = cache_dir / "abcdefghijk_0-8000_v3-video-only-720.mp4"
    _make_source(copy)

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "constraint.json").write_text(json.dumps({"n_clips": 20}))
    (run_dir / "ranked.json").write_text(json.dumps([{"video_id": "abcdefghijk", "priority": "promising"}]))
    rows = [
        {
            "video_id": "abcdefghijk",
            "title": "Colours",
            "priority": "promising",
            "status": "complete",
            "plan_ranges": [[span_offset, span_offset + 8.0]],
            "copies": [
                {
                    "path": str(copy),
                    "span": [span_offset, span_offset + 8.0],
                    "cache_key": copy.name,
                    "size_bytes": copy.stat().st_size,
                    "sha256": sha256_file(copy),
                }
            ],
            "excerpts": [
                {
                    "start_s": span_offset + 2.0,
                    "end_s": span_offset + 8.0,
                    "source_scene": [span_offset + 2.0, span_offset + 8.0],
                    "analysis_span": [span_offset, span_offset + 8.0],
                    "analysis_cache_key": copy.name,
                }
            ],
            "errors": [],
        }
    ]
    excerpts_path = run_dir / "excerpts.json"
    excerpts_path.write_text(json.dumps(rows, indent=2) + "\n")
    (run_dir / "analysis_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "generation_id": "g" * 64,
                "ranked_sha256": sha256_file(run_dir / "ranked.json"),
                "constraint_sha256": sha256_file(run_dir / "constraint.json"),
                "excerpts_sha256": sha256_file(excerpts_path),
                "settings": {},
            }
        )
    )
    return run_dir, copy.name


def test_review_run_extracts_frames_strips_and_packet(tmp_path):
    run_dir, _key = _make_run(tmp_path)
    doc = review_run(run_dir, frames_per_moment=3)

    assert doc["schema_version"] == 1
    assert doc["excerpts_sha256"] == sha256_file(run_dir / "excerpts.json")
    assert doc["counts"] == {"moments": 1, "frames": 3, "errors": 0}
    moment = doc["moments"][0]
    assert (moment["video_id"], moment["excerpt_index"]) == ("abcdefghijk", 0)
    assert len(moment["frames"]) == 3
    assert len(moment["frame_hashes"]) == 3
    expected = [2.05, 5.0, 7.95]
    for frame, t_s in zip(moment["frames"], expected):
        assert frame["t_s"] == round(t_s, 3)
        assert 2.0 <= frame["t_s"] <= 8.0
        assert (run_dir / frame["path"]).is_file()
    assert (run_dir / moment["strip"]).is_file()
    assert (run_dir / "review.json").is_file()

    first_bytes = (run_dir / "review.json").read_bytes()
    second = review_run(run_dir, frames_per_moment=3)
    assert (run_dir / "review.json").read_bytes() == first_bytes
    assert second["moments"][0]["frame_hashes"] == moment["frame_hashes"]


def test_review_run_maps_source_times_into_the_analysis_copy_offset(tmp_path):
    from PIL import Image

    run_dir, _key = _make_run(tmp_path, span_offset=100.0)
    doc = review_run(run_dir, frames_per_moment=3)

    assert doc["counts"] == {"moments": 1, "frames": 3, "errors": 0}, doc["errors"]
    moment = doc["moments"][0]
    assert moment["start_s"] == 102.0 and moment["end_s"] == 108.0
    assert [frame["t_s"] for frame in moment["frames"]] == [102.05, 105.0, 107.95]

    def dominant(frame_path):
        with Image.open(frame_path) as image:
            red, green, blue = image.convert("RGB").getpixel((image.width // 2, image.height // 2))
        return red, green, blue

    first = dominant(run_dir / moment["frames"][0]["path"])
    last = dominant(run_dir / moment["frames"][2]["path"])
    assert first[1] > first[0] and first[1] > first[2]  # local ~2.05s -> green segment
    assert last[0] > last[1] and last[2] > last[1]  # local ~7.95s -> purple segment
    # Solid colours share dHash=0; colour distance still flags the mid-clip cut.
    assert moment.get("continuity_endpoint_color", 0) > 35
    assert moment.get("continuity_suspect") is True


def test_review_run_reports_broken_manifest_cleanly(tmp_path):
    run_dir, _ = _make_run(tmp_path)
    (run_dir / "analysis_manifest.json").write_text("[]", encoding="utf-8")
    with pytest.raises(ShortlistStaleError) as err:
        review_run(run_dir, frames_per_moment=2)
    assert "analysis_manifest.json" in str(err.value)


def test_review_run_reports_broken_excerpts_shape_cleanly(tmp_path):
    run_dir, _ = _make_run(tmp_path)
    excerpts_path = run_dir / "excerpts.json"
    excerpts_path.write_text("{}", encoding="utf-8")
    manifest_path = run_dir / "analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["excerpts_sha256"] = sha256_file(excerpts_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ShortlistStaleError) as err:
        review_run(run_dir, frames_per_moment=2)
    assert "excerpts.json" in str(err.value)


def test_review_run_rejects_non_object_rows(tmp_path):
    run_dir, _ = _make_run(tmp_path)
    excerpts_path = run_dir / "excerpts.json"
    excerpts_path.write_text(json.dumps(["garbage"]), encoding="utf-8")
    manifest_path = run_dir / "analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["excerpts_sha256"] = sha256_file(excerpts_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ShortlistStaleError):
        review_run(run_dir, frames_per_moment=2)


def test_review_run_rejects_non_list_excerpts_field(tmp_path):
    run_dir, _ = _make_run(tmp_path)
    excerpts_path = run_dir / "excerpts.json"
    rows = json.loads(excerpts_path.read_text(encoding="utf-8"))
    rows[0]["excerpts"] = {"not": "a list"}
    excerpts_path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    manifest_path = run_dir / "analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["excerpts_sha256"] = sha256_file(excerpts_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ShortlistStaleError):
        review_run(run_dir, frames_per_moment=2)


def test_review_run_records_build_stage_errors_as_build(tmp_path, monkeypatch):
    import scenery_brief_clips.review as review_module

    run_dir, _ = _make_run(tmp_path)

    def boom(frame_paths, out_path):
        raise RuntimeError("strip failed")

    monkeypatch.setattr(review_module, "_build_strip", boom)
    doc = review_run(run_dir, frames_per_moment=2)
    assert doc["counts"]["errors"] == 1
    assert doc["errors"][0]["stage"] == "build"


def test_review_run_contains_crafted_video_ids_under_the_run_dir(tmp_path):
    run_dir, _ = _make_run(tmp_path)
    excerpts_path = run_dir / "excerpts.json"
    rows = json.loads(excerpts_path.read_text(encoding="utf-8"))
    rows[0]["video_id"] = "../../escape"
    excerpts_path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    manifest_path = run_dir / "analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["excerpts_sha256"] = sha256_file(excerpts_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    doc = review_run(run_dir, frames_per_moment=2)

    assert doc["counts"]["errors"] == 0
    assert not (tmp_path / "escape").exists()
    for moment in doc["moments"]:
        assert moment["strip"].startswith("review/")
        assert (run_dir / moment["strip"]).is_file()
        for frame in moment["frames"]:
            assert frame["path"].startswith("review/")
            assert (run_dir / frame["path"]).is_file()


def test_review_run_uses_collision_free_frame_names(tmp_path):
    run_dir, _ = _make_run(tmp_path)
    excerpts_path = run_dir / "excerpts.json"
    rows = json.loads(excerpts_path.read_text(encoding="utf-8"))
    rows[0]["excerpts"][0]["start_s"] = 4.0
    rows[0]["excerpts"][0]["end_s"] = 4.002
    excerpts_path.write_text(json.dumps(rows, indent=2) + "\n", encoding="utf-8")
    manifest_path = run_dir / "analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["excerpts_sha256"] = sha256_file(excerpts_path)
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    doc = review_run(run_dir, frames_per_moment=4)

    assert doc["counts"]["errors"] == 0
    paths = [frame["path"] for frame in doc["moments"][0]["frames"]]
    assert len(paths) == 4
    assert len(set(paths)) == 4
    for path in paths:
        assert (run_dir / path).is_file()


def test_review_run_records_per_moment_errors_without_failing_whole_run(tmp_path):
    run_dir, key = _make_run(tmp_path)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows[0]["copies"][0]["path"] = str(run_dir / "missing-copy.mp4")
    (run_dir / "excerpts.json").write_text(json.dumps(rows, indent=2) + "\n")
    manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    manifest["excerpts_sha256"] = sha256_file(run_dir / "excerpts.json")
    (run_dir / "analysis_manifest.json").write_text(json.dumps(manifest))

    doc = review_run(run_dir, frames_per_moment=2)

    assert doc["counts"]["moments"] == 0
    assert doc["counts"]["errors"] == 1
    assert doc["errors"][0]["video_id"] == "abcdefghijk"
    assert "missing" in doc["errors"][0]["error"]


def test_review_run_rejects_stale_excerpts(tmp_path):
    run_dir, _key = _make_run(tmp_path)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows[0]["excerpts"][0]["end_s"] = 7.0
    (run_dir / "excerpts.json").write_text(json.dumps(rows, indent=2) + "\n")

    with pytest.raises(ShortlistStaleError):
        review_run(run_dir)
