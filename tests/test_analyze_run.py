import json
from pathlib import Path

import pytest

from scenery_brief_clips.continuity import ContinuitySettings
from scenery_brief_clips.pipeline_analyze import analysis_cache_path, analyze_run, sha256_file


def test_analysis_cache_path_changes_with_source_span(tmp_path):
    first = analysis_cache_path(tmp_path, "abcdefghijk", (10.0, 20.0))
    second = analysis_cache_path(tmp_path, "abcdefghijk", (30.0, 40.0))
    same = analysis_cache_path(tmp_path, "abcdefghijk", (10.0, 20.0))
    millisecond_equivalent = analysis_cache_path(tmp_path, "abcdefghijk", (10.0001, 20.0001))
    next_millisecond = analysis_cache_path(tmp_path, "abcdefghijk", (10.0006, 20.0006))
    assert first != second
    assert first == same == millisecond_equivalent
    assert first != next_millisecond
    assert "10000-20000" in first.name
    assert "30000-40000" in second.name


def test_analyze_refuses_inconsistent_duration_settings(tmp_path):
    from scenery_brief_clips.analyze import ConstraintError

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "abcdefghijk",
                    "priority": "promising",
                    "windows": [],
                    "duration_s": 50.0,
                }
            ]
        )
    )
    (run_dir / "constraint.json").write_text(
        json.dumps({"target_duration_s": -5.0, "duration_min_s": 1.0, "duration_max_s": 12.0})
    )

    def fetch(video_id, dest, span):
        Path(dest).write_bytes(b"video")
        return Path(dest)

    with pytest.raises(ConstraintError):
        analyze_run(
            run_dir,
            cache_dir=tmp_path / "analysis",
            fetch_span=fetch,
            detect_fn=lambda path, min_scene_len_s=0.5: [(0.0, 50.0)],
            max_videos=1,
        continuity_settings=ContinuitySettings(enabled=False),
    )
    assert not (run_dir / "excerpts.json").is_file()


def test_analyze_forwards_min_scene_len_to_detector(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps([{"video_id": "abcdefghijk", "priority": "uncertain", "windows": [], "duration_s": 10.0}])
    )
    (run_dir / "constraint.json").write_text(json.dumps({"duration_min_s": 4, "duration_max_s": 12}))
    seen = []

    def fetch(video_id, dest, span):
        Path(dest).write_bytes(b"video")
        return Path(dest)

    def detect(path, min_scene_len_s):
        seen.append(min_scene_len_s)
        return [(0.0, 10.0)]

    analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=fetch,
        detect_fn=detect,
        min_scene_len_s=0.37,
        continuity_settings=ContinuitySettings(enabled=False),
    )
    assert seen == [0.37]


def test_analyze_run_maps_local_scenes_to_source_time(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "abcdefghijk",
                    "title": "Alps",
                    "priority": "uncertain",
                    "windows": [{"start_s": 100.0, "end_s": 110.0}],
                    "duration_s": 500.0,
                }
            ]
        )
    )
    constraint_path = run_dir / "constraint.json"
    constraint_path.write_text(
        json.dumps(
            {
                "target_duration_s": 6.0,
                "duration_min_s": 4.0,
                "duration_max_s": 12.0,
            }
        )
    )

    def fake_fetch(video_id, dest, span):
        Path(dest).write_bytes(b"not-a-real-video")
        return Path(dest)

    def fake_detect(path, min_scene_len_s=0.5):
        return [(0.0, 8.0)]

    rows = analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=fake_fetch,
        detect_fn=fake_detect,
        max_videos=1,
        max_analysis_s=600,
        continuity_settings=ContinuitySettings(enabled=False),
    )
    assert (run_dir / "excerpts.json").is_file()
    assert rows[0]["video_id"] == "abcdefghijk"
    assert rows[0]["plan_mode"] == "windows"
    excerpts = rows[0]["excerpts"]
    assert excerpts
    # local 0.3-7.7 plus source offset 98 (padded window)
    assert excerpts[0]["start_s"] >= 98.0
    assert excerpts[0]["end_s"] <= 112.0


def test_analyze_records_all_range_outcomes_and_input_provenance(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "abcdefghijk",
                    "title": "Alps",
                    "priority": "promising",
                    "windows": [
                        {"start_s": 100.0, "end_s": 110.0},
                        {"start_s": 200.0, "end_s": 210.0},
                    ],
                    "duration_s": 500.0,
                }
            ]
        )
    )
    (run_dir / "constraint.json").write_text(
        json.dumps({"target_duration_s": 6.0, "duration_min_s": 4.0, "duration_max_s": 12.0})
    )
    attempts = []

    def fetch(video_id, dest, span):
        attempts.append(span)
        if span[0] < 150:
            raise RuntimeError("HTTP 403")
        Path(dest).write_bytes(b"video")
        return Path(dest)

    def detect(path, min_scene_len_s):
        return [(0.0, 8.0)]

    rows = analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=fetch,
        detect_fn=detect,
        max_videos=1,
        acquisition_attempts=1,
        continuity_settings=ContinuitySettings(enabled=False),
    )
    row = rows[0]
    assert row["status"] == "partial"
    assert [r["status"] for r in row["ranges"]] == ["failed", "complete"]
    assert row["ranges"][0]["stage"] == "acquire"
    assert "403" in row["ranges"][0]["error"]
    assert len(row["errors"]) == 1
    manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    assert manifest["schema_version"] == 3
    assert len(manifest["ranked_sha256"]) == 64
    assert len(manifest["constraint_sha256"]) == 64
    assert manifest["excerpts_sha256"] == sha256_file(run_dir / "excerpts.json")
    assert len(manifest["generation_id"]) >= 16
    assert manifest["settings"]["cache_policy"] == "v3-video-only-720"


def test_input_change_during_analysis_aborts_generation_publication(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ranked_path = run_dir / "ranked.json"
    ranked = [
        {
            "video_id": "abcdefghijk",
            "priority": "promising",
            "windows": [],
            "duration_s": 10.0,
        }
    ]
    ranked_path.write_text(json.dumps(ranked))
    (run_dir / "constraint.json").write_text(json.dumps({"duration_min_s": 4, "duration_max_s": 12}))
    old_excerpts = "old excerpts\n"
    old_manifest = "old manifest\n"
    (run_dir / "excerpts.json").write_text(old_excerpts)
    (run_dir / "analysis_manifest.json").write_text(old_manifest)

    def fetch(_video_id, dest, _span):
        changed = json.loads(ranked_path.read_text())
        changed[0]["duration_s"] = 20.0
        ranked_path.write_text(json.dumps(changed))
        Path(dest).write_bytes(b"video")
        return Path(dest)

    with pytest.raises(RuntimeError, match="inputs changed during analysis"):
        analyze_run(
            run_dir,
            cache_dir=tmp_path / "analysis",
            fetch_span=fetch,
            detect_fn=lambda _path, min_scene_len_s: [(0.0, 10.0)],
            acquisition_attempts=1,
        continuity_settings=ContinuitySettings(enabled=False),
    )

    assert (run_dir / "excerpts.json").read_text() == old_excerpts
    assert (run_dir / "analysis_manifest.json").read_text() == old_manifest


def test_all_failed_ranges_mark_video_failed(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps([{"video_id": "abcdefghijk", "priority": "promising", "windows": [], "duration_s": 10.0}])
    )
    (run_dir / "constraint.json").write_text(json.dumps({"duration_min_s": 4, "duration_max_s": 12}))

    rows = analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=lambda *args: (_ for _ in ()).throw(RuntimeError("down")),
        detect_fn=lambda path, min_scene_len_s: [(0.0, 10.0)],
        acquisition_attempts=1,
        continuity_settings=ContinuitySettings(enabled=False),
    )
    assert rows[0]["status"] == "failed"
    assert rows[0]["excerpts"] == []


def test_invalid_detector_coordinates_fail_the_range_instead_of_fabricating_excerpt(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps([{"video_id": "abcdefghijk", "priority": "promising", "windows": [], "duration_s": 10.0}])
    )
    (run_dir / "constraint.json").write_text(json.dumps({"duration_min_s": 4, "duration_max_s": 12}))

    def fetch(_video_id, dest, _span):
        Path(dest).write_bytes(b"video")
        return Path(dest)

    rows = analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=fetch,
        detect_fn=lambda _path, min_scene_len_s: [(float("nan"), float("nan"))],
        acquisition_attempts=1,
        continuity_settings=ContinuitySettings(enabled=False),
    )

    assert rows[0]["status"] == "failed"
    assert rows[0]["errors"][0]["stage"] == "detect"
    assert rows[0]["excerpts"] == []


def test_detect_failure_invalidates_and_retries_while_preserving_attempt_error(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps([{"video_id": "abcdefghijk", "priority": "promising", "windows": [], "duration_s": 10.0}])
    )
    (run_dir / "constraint.json").write_text(json.dumps({"duration_min_s": 4, "duration_max_s": 12}))
    fetches = 0
    invalidated = []

    def fetch(_video_id, dest, _span):
        nonlocal fetches
        fetches += 1
        Path(dest).write_bytes(b"bad" if fetches == 1 else b"good")
        return Path(dest)

    def detect(path, min_scene_len_s):
        if Path(path).read_bytes() == b"bad":
            raise RuntimeError("decode failed")
        return [(0.0, 10.0)]

    def invalidate(path):
        invalidated.append(Path(path))
        Path(path).unlink(missing_ok=True)

    rows = analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=fetch,
        detect_fn=detect,
        invalidate_span=invalidate,
        acquisition_attempts=2,
        continuity_settings=ContinuitySettings(enabled=False),
    )

    assert rows[0]["status"] == "complete"
    assert fetches == 2
    assert len(invalidated) == 1
    assert rows[0]["ranges"][0]["attempt_errors"][0]["stage"] == "detect"
    assert rows[0]["excerpts"]


def test_disappearing_analysis_file_becomes_recorded_range_failure(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps([{"video_id": "abcdefghijk", "priority": "promising", "windows": [], "duration_s": 10.0}])
    )
    (run_dir / "constraint.json").write_text(json.dumps({"duration_min_s": 4, "duration_max_s": 12}))

    def fetch(_video_id, dest, _span):
        Path(dest).write_bytes(b"video")
        return Path(dest)

    def detect(path, min_scene_len_s):
        Path(path).unlink()
        return [(0.0, 10.0)]

    rows = analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=fetch,
        detect_fn=detect,
        acquisition_attempts=1,
        continuity_settings=ContinuitySettings(enabled=False),
    )

    assert rows[0]["status"] == "failed"
    assert rows[0]["errors"][0]["stage"] == "record"
    assert "No such file" in rows[0]["errors"][0]["error"]
    assert (run_dir / "excerpts.json").is_file()


def test_analyze_run_skips_low_priority(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps([{"video_id": "abcdefghijk", "priority": "low", "windows": [], "duration_s": 50}])
    )
    (run_dir / "constraint.json").write_text(json.dumps({"target_duration_s": 6}))
    called = []

    def fake_fetch(video_id, dest, span):
        called.append(video_id)
        return Path(dest)

    rows = analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=fake_fetch,
        detect_fn=lambda p, min_scene_len_s=0.5: [(0.0, 10.0)],
        max_videos=3,
        continuity_settings=ContinuitySettings(enabled=False),
    )
    assert called == []
    assert rows[0]["skipped"] == "low"


def test_local_nonzero_offset_analysis_verifies_real_media_generation(tmp_path):
    import subprocess

    from scenery_brief_clips.detect import detect_scenes
    from scenery_brief_clips.verify import verify_run
    from scenery_brief_clips.yt import YtDlp

    source = tmp_path / "source.mp4"
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
            "color=c=red:s=320x180:d=10",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:d=10",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-pix_fmt",
            "yuv420p",
            str(source),
        ],
        check=True,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "constraint.json").write_text(
        json.dumps(
            {
                "min_width": 1920,
                "min_height": 1080,
                "aspect_min": 1.7,
                "aspect_max": 1.86,
                "target_duration_s": 6.0,
                "duration_min_s": 4.0,
                "duration_max_s": 12.0,
            }
        )
    )
    (run_dir / "candidates.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "localtestxxx",
                    "width": 1920,
                    "height": 1080,
                    "aspect": 16 / 9,
                }
            ]
        )
    )
    (run_dir / "ranked.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "localtestxxx",
                    "title": "colors",
                    "priority": "promising",
                    "windows": [{"start_s": 10.0, "end_s": 20.0}],
                    "duration_s": 20.0,
                }
            ]
        )
    )

    def runner(cmd, **kwargs):
        output = Path(cmd[cmd.index("-o") + 1])
        result = subprocess.run(
            [
                "ffmpeg",
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                "10",
                "-i",
                str(source),
                "-t",
                "10",
                "-an",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                str(output),
            ],
            capture_output=True,
            text=True,
        )
        return subprocess.CompletedProcess(cmd, result.returncode, stdout="", stderr=result.stderr)

    analysis_dir = tmp_path / "analysis"
    yt = YtDlp(tmp_dir=tmp_path / "tmp", runner=runner)
    rows = analyze_run(
        run_dir,
        cache_dir=analysis_dir,
        fetch_span=lambda video_id, dest, span: yt.fetch_analysis(video_id, dest, span),
        detect_fn=detect_scenes,
        max_videos=1,
        max_analysis_s=20,
        pad_s=0,
        invalidate_span=yt.invalidate_analysis,
        continuity_settings=ContinuitySettings(enabled=False),
    )

    assert rows[0]["status"] == "complete"
    assert rows[0]["plan_ranges"] == [[10.0, 20.0]]
    assert rows[0]["excerpts"]
    assert all(10.0 <= excerpt["start_s"] < excerpt["end_s"] <= 20.0 for excerpt in rows[0]["excerpts"])
    report = verify_run(run_dir, analysis_dir=analysis_dir)
    assert report["ok"] is True, report


def test_analyze_run_local_file_detects_and_excerpts(tmp_path):
    import subprocess

    from scenery_brief_clips.detect import detect_scenes

    video = tmp_path / "src.mp4"
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
            "color=c=red:s=320x180:d=8",
            "-f",
            "lavfi",
            "-i",
            "color=c=blue:s=320x180:d=8",
            "-filter_complex",
            "[0:v][1:v]concat=n=2:v=1:a=0",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
    )
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "localtestxxx",
                    "title": "colors",
                    "priority": "uncertain",
                    "windows": [],
                    "duration_s": 16.0,
                }
            ]
        )
    )
    (run_dir / "constraint.json").write_text(
        json.dumps({"target_duration_s": 6.0, "duration_min_s": 4.0, "duration_max_s": 12.0})
    )

    def fetch(video_id, dest, span):
        dest = Path(dest)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(video.read_bytes())
        return dest

    rows = analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=fetch,
        detect_fn=detect_scenes,
        max_videos=1,
        max_analysis_s=600,
        continuity_settings=ContinuitySettings(enabled=False),
    )
    assert rows[0]["plan_mode"] == "full"
    assert len(rows[0]["excerpts"]) >= 1
    for excerpt in rows[0]["excerpts"]:
        assert excerpt["end_s"] - excerpt["start_s"] >= 4.0


def test_analyze_workers_env_default_and_override(monkeypatch):
    from scenery_brief_clips.pipeline_analyze import _analyze_workers

    monkeypatch.delenv("SCENERY_ANALYZE_WORKERS", raising=False)
    assert _analyze_workers() == 4
    monkeypatch.setenv("SCENERY_ANALYZE_WORKERS", "1")
    assert _analyze_workers() == 1
    monkeypatch.setenv("SCENERY_ANALYZE_WORKERS", "0")
    assert _analyze_workers() == 1  # clamped


def test_detect_frame_skip_env_default(monkeypatch):
    from scenery_brief_clips.detect import _detect_frame_skip

    monkeypatch.delenv("SCENERY_DETECT_FRAME_SKIP", raising=False)
    assert _detect_frame_skip() == 1
    monkeypatch.setenv("SCENERY_DETECT_FRAME_SKIP", "0")
    assert _detect_frame_skip() == 0
