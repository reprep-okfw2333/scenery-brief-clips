import json
from pathlib import Path

import pytest

from scenery_brief_clips.cli import main
from scenery_brief_clips.config import ConfigError, load_project_config
from scenery_brief_clips.pipeline import DryRunResult


def test_flat_project_config_loads_and_rejects_unsafe_or_unknown_values(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text(
        "allow_download: false\n"
        "max_search_results: 7\n"
        "sleep_s: 0.5\n"
        "max_tiles: 4\n"
        "# comment\n"
    )
    loaded = load_project_config(tmp_path)
    assert loaded == {
        "allow_download": False,
        "max_search_results": 7,
        "sleep_s": 0.5,
        "max_tiles": 4,
    }

    config.write_text("allow_download: true\n")
    with pytest.raises(ConfigError, match="allow_download must remain false"):
        load_project_config(tmp_path)

    config.write_text("mystery_option: 1\n")
    with pytest.raises(ConfigError, match="unknown config key"):
        load_project_config(tmp_path)


def test_checked_in_example_config_is_loadable():
    root = Path(__file__).resolve().parents[1]
    loaded = load_project_config(root, root / "config.example.yaml")
    assert loaded["allow_download"] is False
    assert loaded["allow_export"] is False
    assert loaded["max_search_results"] == 20
    assert loaded["max_analysis_s"] == 120.0


def test_allow_export_gate_defaults_off_and_requires_a_boolean(tmp_path):
    config = tmp_path / "config.yaml"
    config.write_text("allow_download: false\nallow_export: true\n")
    assert load_project_config(tmp_path)["allow_export"] is True

    config.write_text("allow_export: false\n")
    assert load_project_config(tmp_path)["allow_export"] is False

    config.write_text("allow_export: 1\n")
    with pytest.raises(ConfigError, match="allow_export must be a boolean"):
        load_project_config(tmp_path)


def test_export_max_height_validates_bounds_and_types(tmp_path):
    config = tmp_path / "config.yaml"
    for good in ("720", "1080", "2160"):
        config.write_text(f"export_max_height: {good}\n")
        assert load_project_config(tmp_path)["export_max_height"] == int(good)
    for bad in ("480", "true", "720.5", "null", "0", "3000", "auto"):
        config.write_text(f"export_max_height: {bad}\n")
        with pytest.raises(ConfigError):
            load_project_config(tmp_path)


def test_analyze_uses_config_defaults_and_explicit_flags_override(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text("[]")
    (tmp_path / "config.yaml").write_text(
        "allow_download: false\nmax_analysis_s: 33\nmax_analyze_videos: 2\n"
    )
    calls = []

    def fake_analyze(*args, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr("scenery_brief_clips.cli.analyze_run", fake_analyze)
    first = main(["analyze", "--run-dir", str(run_dir), "--root", str(tmp_path)])
    assert first == 0
    json.loads(capsys.readouterr().out)
    assert calls[-1]["max_videos"] == 2
    assert calls[-1]["max_analysis_s"] == 33

    second = main(
        [
            "analyze",
            "--run-dir",
            str(run_dir),
            "--root",
            str(tmp_path),
            "--max-videos",
            "1",
            "--max-analysis-s",
            "44",
        ]
    )
    assert second == 0
    json.loads(capsys.readouterr().out)
    assert calls[-1]["max_videos"] == 1
    assert calls[-1]["max_analysis_s"] == 44


def test_run_config_tightens_geometry_and_flags_override(tmp_path, monkeypatch, capsys):
    (tmp_path / "config.yaml").write_text(
        "allow_download: false\n"
        "min_width: 2560\n"
        "min_height: 1440\n"
        "aspect_min: 1.75\n"
        "aspect_max: 1.80\n"
        "max_search_results: 3\n"
        "sleep_s: 0.5\n"
    )
    captured = []

    def fake_run_dry(constraint, **kwargs):
        captured.append(constraint)
        return DryRunResult(queries=[], stopped_reason="complete")

    monkeypatch.setattr("scenery_brief_clips.cli.run_dry", fake_run_dry)
    code = main(
        ["run", "--dry-run", "--prompt", "alps 1080p 16:9 2 clips", "--root", str(tmp_path)]
    )
    assert code == 0
    json.loads(capsys.readouterr().out)
    constraint = captured[-1]
    assert constraint.min_width == 2560
    assert constraint.min_height == 1440
    assert constraint.aspect_min == 1.75
    assert constraint.aspect_max == 1.80
    assert constraint.limits.max_search_results == 3
    assert constraint.limits.sleep_s == 0.5
    assert constraint.allow_download is False

    code = main(
        [
            "run",
            "--dry-run",
            "--prompt",
            "alps 1080p 16:9 2 clips",
            "--root",
            str(tmp_path),
            "--max-results",
            "7",
            "--sleep",
            "0",
        ]
    )
    assert code == 0
    json.loads(capsys.readouterr().out)
    assert captured[-1].limits.max_search_results == 7
    assert captured[-1].limits.sleep_s == 0.0


def test_run_rejects_config_that_conflicts_with_prompt_geometry(tmp_path, capsys):
    (tmp_path / "config.yaml").write_text(
        "allow_download: false\naspect_min: 1.9\naspect_max: 2.0\n"
    )
    code = main(
        ["run", "--dry-run", "--prompt", "alps 1080p 16:9 2 clips", "--root", str(tmp_path)]
    )
    assert code == 2


def test_run_rejects_config_path_outside_project_root(tmp_path, capsys):
    root = tmp_path / "proj"
    root.mkdir()
    outside = tmp_path / "outside.yaml"
    outside.write_text("allow_download: false\nmax_search_results: 3\n")

    code = main(
        [
            "run",
            "--dry-run",
            "--prompt",
            "alps 1080p 16:9 2 clips",
            "--root",
            str(root),
            "--config",
            str(outside),
        ]
    )
    assert code == 2


def test_rank_uses_config_defaults_and_flags_override(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "candidates.json").write_text(
        json.dumps([{"video_id": "abcdefghijk", "title": "Alps"}])
    )
    (tmp_path / "config.yaml").write_text(
        "allow_download: false\nmax_rank_videos: 2\nmax_tiles: 3\n"
    )
    calls = []

    def fake_rank_run(*args, **kwargs):
        calls.append(kwargs)
        return []

    monkeypatch.setattr("scenery_brief_clips.cli.rank_run", fake_rank_run)
    code = main(["rank", "--run-dir", str(run_dir), "--root", str(tmp_path)])
    assert code == 0
    json.loads(capsys.readouterr().out)
    assert calls[-1]["max_videos"] == 2
    assert calls[-1]["max_tiles"] == 3

    code = main(
        [
            "rank",
            "--run-dir",
            str(run_dir),
            "--root",
            str(tmp_path),
            "--max-videos",
            "5",
            "--max-tiles",
            "9",
        ]
    )
    assert code == 0
    json.loads(capsys.readouterr().out)
    assert calls[-1]["max_videos"] == 5
    assert calls[-1]["max_tiles"] == 9
