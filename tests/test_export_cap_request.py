"""The brief's export cap reaches the standalone commands via discovery.json.

run-brief and the runner record the brief's export_max_height in
discovery.json; `export` and `analyze` apply it with the same rule as
run-pipeline (the brief asks, config may only lower). Runs without the key
(legacy `run`, or made before the key existed) keep the config-only cap.
"""

import json
from pathlib import Path

import pytest

from scenery_brief_clips.cli import main
from scenery_brief_clips.export import (
    ExportError,
    config_with_requested_export_cap,
    requested_export_cap,
)

from test_brief import valid_brief
from test_run_brief_cli import FakeYt, _hd_info, _wire_fake_yt, _write_brief_and_plan


def _discovery(run_dir: Path, **fields) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    doc = {"schema_version": "brief_discovery_v1", "brief_sha256": "0" * 64, **fields}
    (run_dir / "discovery.json").write_text(json.dumps(doc), encoding="utf-8")


# requested_export_cap -------------------------------------------------------

def test_no_discovery_means_no_request(tmp_path):
    assert requested_export_cap(tmp_path) is None


def test_discovery_without_key_means_no_request(tmp_path):
    _discovery(tmp_path)
    assert requested_export_cap(tmp_path) is None


@pytest.mark.parametrize("height", [720, 1080])
def test_discovery_key_is_returned(tmp_path, height):
    _discovery(tmp_path, export_max_height=height)
    assert requested_export_cap(tmp_path) == height


@pytest.mark.parametrize("bad", [1440, 480, "1080", 1080.0, True, None])
def test_invalid_recorded_cap_fails_closed(tmp_path, bad):
    _discovery(tmp_path, export_max_height=bad)
    with pytest.raises(ExportError):
        requested_export_cap(tmp_path)


def test_unreadable_discovery_fails_closed(tmp_path):
    (tmp_path / "discovery.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(ExportError):
        requested_export_cap(tmp_path)


# config_with_requested_export_cap ------------------------------------------

def test_no_request_returns_config_unchanged():
    config = {"allow_export": True}
    assert config_with_requested_export_cap(config, None) is config


def test_request_equal_to_resolved_cap_returns_same_object():
    config = {}
    assert config_with_requested_export_cap(config, 720) is config


def test_request_1080_raises_default_cap_in_a_copy():
    config = {"allow_export": True}
    out = config_with_requested_export_cap(config, 1080)
    assert out == {"allow_export": True, "export_max_height": 1080}
    assert config == {"allow_export": True}


def test_config_may_lower_request():
    config = {"export_max_height": 720}
    assert config_with_requested_export_cap(config, 1080)["export_max_height"] == 720


def test_config_may_not_raise_request():
    config = {"export_max_height": 1080}
    out = config_with_requested_export_cap(config, 720)
    assert out["export_max_height"] == 720
    assert config == {"export_max_height": 1080}


# run-brief records the cap --------------------------------------------------

@pytest.mark.parametrize("height", [720, 1080])
def test_run_brief_records_export_cap(tmp_path, monkeypatch, capsys, height):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    if height == 1080:
        brief["export_max_height"] = 1080
        brief["source_geometry"].update(min_width=1920, min_height=1080)
        brief_path.write_text(json.dumps(brief), encoding="utf-8")
        from test_brief import valid_plan

        plan_path.write_text(json.dumps(valid_plan(brief)), encoding="utf-8")
    _wire_fake_yt([("fixture_ok", "Alpaca field")], {"fixture_ok": _hd_info("fixture_ok", "Alpaca field")})
    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", FakeYt)

    code = main(
        [
            "run-brief", "--brief", str(brief_path), "--plan", str(plan_path),
            "--dry-run", "--root", str(tmp_path), "--sleep", "0",
        ]
    )
    assert code == 0
    run_dir = Path(json.loads(capsys.readouterr().out)["run_dir"])
    discovery = json.loads((run_dir / "discovery.json").read_text(encoding="utf-8"))
    assert discovery["export_max_height"] == height
    assert requested_export_cap(run_dir) == height


# export CLI applies it -----------------------------------------------------

def _capture_export(monkeypatch):
    seen = {}

    def fake_export(run_dir_arg, root_arg, theme=None, allow_export=False, config=None):
        seen["config"] = config
        return {"theme": "t", "counts": {"failed": 0, "exported": 0}, "failed": []}

    monkeypatch.setattr("scenery_brief_clips.cli.export_run", fake_export)
    return seen


def test_export_cli_applies_recorded_1080_request(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    _discovery(run_dir, export_max_height=1080)
    seen = _capture_export(monkeypatch)
    code = main(["export", "--run-dir", str(run_dir), "--root", str(tmp_path), "--allow-export"])
    assert code == 0
    assert seen["config"]["export_max_height"] == 1080


def test_export_cli_config_lowers_recorded_request(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    _discovery(run_dir, export_max_height=1080)
    (tmp_path / "config.yaml").write_text("export_max_height: 720\n")
    seen = _capture_export(monkeypatch)
    assert main(["export", "--run-dir", str(run_dir), "--root", str(tmp_path), "--allow-export"]) == 0
    assert seen["config"]["export_max_height"] == 720


def test_export_cli_without_record_keeps_config(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (tmp_path / "config.yaml").write_text("export_max_height: 1080\n")
    seen = _capture_export(monkeypatch)
    assert main(["export", "--run-dir", str(run_dir), "--root", str(tmp_path), "--allow-export"]) == 0
    assert seen["config"] == {"export_max_height": 1080}


def test_export_cli_invalid_record_exits_2(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    _discovery(run_dir, export_max_height=1440)
    seen = _capture_export(monkeypatch)
    assert main(["export", "--run-dir", str(run_dir), "--root", str(tmp_path), "--allow-export"]) == 2
    assert "config" not in seen
    assert "export_max_height" in capsys.readouterr().err


# runner discovery records it too --------------------------------------------

def test_runner_and_cli_share_the_rule():
    from scenery_brief_clips.runner import _config_with_brief_export_cap

    brief = valid_brief()
    brief["export_max_height"] = 1080
    for config in ({}, {"export_max_height": 720}, {"export_max_height": 1080}, {"allow_export": True}):
        assert _config_with_brief_export_cap(config, brief) == config_with_requested_export_cap(config, 1080)
