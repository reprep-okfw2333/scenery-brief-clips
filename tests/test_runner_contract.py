"""Runner contract: real stages, fakes only at the outside edge."""

from __future__ import annotations

import fcntl
import json
import socket
from io import BytesIO
from pathlib import Path

import pytest
from PIL import Image

from scenery_brief_clips.brief import canonical_json_hash
from scenery_brief_clips.cli import main
from scenery_brief_clips.runner import Ports, advance
from test_brief import valid_brief, valid_plan

VIDEO = "abcdefghijk"


def test_runner_module_exposes_advance():
    assert callable(advance)


def _jpeg() -> bytes:
    image = Image.new("RGB", (32, 18), (40, 140, 60))
    buf = BytesIO()
    image.save(buf, format="JPEG")
    return buf.getvalue()


def _metadata() -> dict:
    return {
        "id": VIDEO,
        "title": "alpacas in a field",
        "duration": 120,
        "live_status": "not_live",
        "availability": "public",
        "formats": [
            {
                "format_id": "136",
                "vcodec": "avc1",
                "acodec": "none",
                "width": 1280,
                "height": 720,
                "fps": 30,
            },
            {
                "format_id": "sb0",
                "format_note": "storyboard",
                "protocol": "mhtml",
                "width": 32,
                "height": 18,
                "fps": 0.1,
                "rows": 1,
                "columns": 1,
                "fragments": [{"url": "http://storyboard.test/sheet.jpg"}],
            },
        ],
    }


class FakeYt:
    def __init__(self):
        self.searches = 0
        self.exports = 0

    def search(self, query, limit):
        self.searches += 1
        return [{"id": VIDEO, "title": "alpacas in a field"}]

    def fetch_metadata(self, video_id):
        return _metadata()

    def fetch_export(self, spec, timeout=900):
        self.exports += 1
        dest = Path(getattr(spec, "dest", None) or "/tmp/should-not-be-used")
        return dest


class Guard:
    def __init__(self):
        self.tripped = []

    def install(self, monkeypatch):
        guard = self

        def boom(*args, **kwargs):
            guard.tripped.append("network")
            raise AssertionError("unexpected network")

        def no_subprocess(*args, **kwargs):
            guard.tripped.append(args[0] if args else "subprocess")
            raise AssertionError(f"unexpected media tool: {args[:1]}")

        monkeypatch.setattr(socket, "create_connection", boom)
        monkeypatch.setattr("subprocess.run", no_subprocess)


def _root(tmp_path: Path) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "vision.yaml").write_text(
        "backend: codex-login\nmodel: gpt-6-sol\n",
        encoding="utf-8",
    )
    (root / "config.yaml").write_text(
        "\n".join(
            [
                "continuity_enabled: false",
                "max_analyze_videos: 1",
                "max_tiles: 2",
                "max_rank_videos: 1",
                "max_analysis_s: 120",
                "export_max_height: 720",
                "jev_gate: true",
                "sleep_s: 0",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (root / "tmp").mkdir()
    return root


def _brief_plan(root: Path):
    brief = valid_brief()
    plan = valid_plan(brief)
    brief_path = root / "brief.json"
    plan_path = root / "plan.json"
    brief_path.write_text(json.dumps(brief), encoding="utf-8")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return brief_path, plan_path


def _ports(yt, **kwargs) -> Ports:
    def fetch_span(video_id, dest, span):
        from scenery_brief_clips.analysis_cache import (
            ANALYSIS_CACHE_POLICY,
            ANALYSIS_MARKER_SCHEMA_VERSION,
            analysis_marker_path,
            canonical_span_ms,
            sha256_file,
        )

        dest_path = Path(dest)
        dest_path.write_bytes(b"fake-analysis")
        start_ms, end_ms = canonical_span_ms(span)
        marker = {
            "schema_version": ANALYSIS_MARKER_SCHEMA_VERSION,
            "cache_policy": ANALYSIS_CACHE_POLICY,
            "video_id": video_id,
            "span_ms": [start_ms, end_ms],
            "size_bytes": dest_path.stat().st_size,
            "sha256": sha256_file(dest_path),
        }
        analysis_marker_path(dest_path).write_text(json.dumps(marker), encoding="utf-8")
        return dest_path

    def detect_fn(path, min_scene_len_s=0.5):
        return [(0.0, 8.0)]

    def fetcher(url):
        return _jpeg()

    def probe(path):
        return {
            "video_streams": 1,
            "audio_streams": 0,
            "width": 1280,
            "height": 720,
            "duration_s": 10.0,
        }

    def decode(path):
        return None

    def export_probe(path, window_ms=None):
        from scenery_brief_clips.export import expected_clip_frames

        local_start = 0.3
        local_end = 7.7
        fps_num, fps_den = 30, 1
        frames = expected_clip_frames(local_start, local_end, fps_num, fps_den)
        return {
            "width": 1280,
            "height": 720,
            "first_pts_s": 0.0,
            "last_end_s": local_end - local_start,
            "fps_num": fps_num,
            "fps_den": fps_den,
            "n_frames": frames,
            "max_gap_s": 0.03,
            "window_n_frames": frames,
            "window_max_gap_s": 0.03,
        }

    return Ports(
        yt=yt,
        sleep_fn=lambda _s: None,
        rank_fetcher=fetcher,
        fetch_span=fetch_span,
        detect_fn=detect_fn,
        verify_probe=probe,
        verify_decode=decode,
        export_probe=export_probe,
        clock=kwargs.get("clock") or (lambda: 0.0),
        tile_caller=kwargs.get("tile_caller"),
        strip_caller=kwargs.get("strip_caller"),
        planner_caller=kwargs.get("planner_caller"),
    )


def _tile_text(wire, path, prompt):
    return json.dumps({"label": "keep", "look": "europe_like", "note": "field"})


def _strip_text(wire, path, prompt):
    return json.dumps(
        {
            "match": "keep",
            "geo": "uncertain",
            "scene_type": "field",
            "note": "alpacas",
            "continuity_ok": True,
        }
    )


def test_frozen_plan_pauses_for_vision_and_does_not_call_planner(tmp_path, monkeypatch):
    guard = Guard()
    guard.install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    planner_calls = []

    def planner(*args, **kwargs):
        planner_calls.append(args)
        raise AssertionError("planner must not be called")

    yt = FakeYt()
    result = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        ports=_ports(yt, planner_caller=planner),
    )
    assert result["status"] == "paused"
    assert result["stage"] == "agree_vision"
    assert result["vision_model"]["model"] == "gpt-6-sol"
    assert result["vision_model"]["backend"] == "codex-login"
    assert result["jev"]["enabled"] is False
    assert planner_calls == []
    assert yt.exports == 0
    assert guard.tripped == []
    state = json.loads((Path(result["run_dir"]) / "runner_state.json").read_text(encoding="utf-8"))
    assert "label_tiles" not in state["completed"]


def test_agreement_is_required_and_a_model_change_cancels_it(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    first = advance(root, brief=brief_path, plan=plan_path, ports=_ports(yt))
    second = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        run_dir=first["run_dir"],
        vision_agree=True,
        ports=_ports(yt, tile_caller=_tile_text),
    )
    assert second["stage"] != "agree_vision"
    (root / "vision.yaml").write_text("backend: codex-login\nmodel: other-model\n", encoding="utf-8")
    third = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        run_dir=first["run_dir"],
        ports=_ports(yt, tile_caller=_tile_text),
    )
    assert third["status"] == "paused"
    assert third["stage"] == "agree_vision"
    assert third["vision_model"]["model"] == "other-model"


def test_export_requires_allowance_and_jev_stays_off(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    monkeypatch.setattr(
        "scenery_brief_clips.jev_gate.gate_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("jev must not run")),
    )
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    paused = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        vision_agree=True,
        ports=_ports(yt, tile_caller=_tile_text, strip_caller=_strip_text),
    )
    assert paused["status"] in {"paused", "failed", "recovery", "completed"}
    assert yt.exports == 0
    if paused["status"] == "paused":
        assert paused["stage"] in {"agree_export", "label_strips", "analyze", "verify_review", "shortlist_review"}


def test_interrupted_external_stage_pauses_for_recovery(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    run_dir = root / "data" / "runs" / "interrupted"
    run_dir.mkdir(parents=True)
    (run_dir / "runner_inflight.json").write_text(
        json.dumps({"stage": "discover", "binding": "x"}),
        encoding="utf-8",
    )
    yt = FakeYt()
    result = advance(root, brief=brief_path, plan=plan_path, run_dir=run_dir, ports=_ports(yt))
    assert result["status"] == "recovery"
    assert result["stage"] == "discover"
    assert "acknowledge_uncertain=discover" in result["how_to_supply"]
    assert yt.searches == 0


def test_second_runner_cannot_modify_a_locked_run(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    run_dir = root / "data" / "runs" / "locked"
    run_dir.mkdir(parents=True)
    state_path = run_dir / "runner_state.json"
    state_path.write_text("{}\n", encoding="utf-8")
    before = state_path.read_bytes()
    handle = (run_dir / "runner.lock").open("a+")
    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    try:
        result = advance(root, brief=brief_path, plan=plan_path, run_dir=run_dir, ports=_ports(FakeYt()))
    finally:
        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        handle.close()
    assert result["status"] == "locked"
    assert state_path.read_bytes() == before


def test_cli_entry_points_at_advance(tmp_path, monkeypatch):
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    called = {}

    def fake_advance(*args, **kwargs):
        called["kwargs"] = kwargs
        return {"status": "paused", "stage": "agree_vision", "run_dir": str(root), "jev": {"enabled": False}}

    monkeypatch.setattr("scenery_brief_clips.runner.advance", fake_advance)
    code = main(
        [
            "run-pipeline",
            "--root",
            str(root),
            "--brief",
            str(brief_path),
            "--plan",
            str(plan_path),
        ]
    )
    assert code == 0
    assert called["kwargs"]["plan"] == plan_path or str(called["kwargs"]["plan"]) == str(plan_path)


def _patch_media(monkeypatch, yt: FakeYt):
    calls = {"encode": 0}

    def fetch_export(spec, timeout=900):
        yt.exports += 1
        dest = yt.base / f"acq-{yt.exports}.mp4"
        dest.write_bytes(b"acquisition-bytes")
        return dest

    yt.fetch_export = fetch_export

    def encode_clip(src, start, end, dest):
        calls["encode"] += 1
        Path(dest).write_bytes(b"clip-bytes")

    def probe_export_coverage(path, window_ms=None):
        return {
            "width": 1280,
            "height": 720,
            "first_pts_s": 0.0,
            "last_end_s": 10000.0,
            "n_frames": 180,
            "window_n_frames": 180,
            "window_max_gap_s": 0.03,
        }

    def validate_final(path, spec, start, end, max_height):
        return {"width": 1280, "height": 720, "last_end_s": float(end) - float(start), "n_frames": 180}

    def extract_frame(cache_path, t_s, out_path):
        out_path.write_bytes(_jpeg())

    monkeypatch.setattr("scenery_brief_clips.export.encode_clip", encode_clip)
    monkeypatch.setattr("scenery_brief_clips.export.probe_export_coverage", probe_export_coverage)
    monkeypatch.setattr("scenery_brief_clips.export._validate_final_clip", validate_final)
    monkeypatch.setattr("scenery_brief_clips.export._ffmpeg_version", lambda: "fake-ffmpeg")
    monkeypatch.setattr("scenery_brief_clips.review._extract_frame", extract_frame)
    return calls


def test_authorized_run_reaches_fake_export_and_reuses_it(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / "acq"
    yt.base.mkdir()
    calls = _patch_media(monkeypatch, yt)
    _tile_text.last_usage = {"total_tokens": 4}
    ports = _ports(yt, tile_caller=_tile_text, strip_caller=_strip_text)
    first = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        vision_agree=True,
        allow_export=True,
        theme="contract-theme",
        ports=ports,
    )
    assert first["status"] == "completed", first
    assert calls["encode"] == 1
    assert yt.exports == 1
    searches_after_first = yt.searches
    assert searches_after_first == 2

    report = json.loads((Path(first["run_dir"]) / "verify_export.json").read_text(encoding="utf-8"))
    assert report["ok"] is True
    timing = first["timing"]["stages"]
    assert any(item["status"] == "executed" for item in timing)
    tile_rows = [item for item in timing if item["stage"] == "label_tiles"]
    assert tile_rows[-1]["tokens"] == 4

    second = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        run_dir=first["run_dir"],
        vision_agree=True,
        allow_export=True,
        theme="contract-theme",
        ports=ports,
    )
    assert second["status"] == "completed", second
    assert yt.exports == 1
    assert calls["encode"] == 1
    assert yt.searches == searches_after_first
    assert any(item["status"] == "reused" for item in second["timing"]["stages"])
    assert all(item["tokens"] is None or item["status"] != "reused" for item in second["timing"]["stages"])


def test_stage_failure_stops_before_later_stages(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()

    def boom(video_id):
        raise RuntimeError("metadata down")

    yt.fetch_metadata = boom
    fetches = {"n": 0}

    def fetcher(url):
        fetches["n"] += 1
        return _jpeg()

    ports = _ports(yt)
    ports.rank_fetcher = fetcher
    result = advance(root, brief=brief_path, plan=plan_path, ports=ports)
    assert result["status"] == "failed"
    assert result["stage"] == "discover"
    assert "metadata_errors" in result["error"]
    assert fetches["n"] == 0
    failed = [item for item in result["timing"]["stages"] if item["status"] == "failed"]
    assert failed[-1]["stage"] == "discover"
    assert failed[-1]["tokens"] is None


def test_sleep_change_reuses_search_and_rank_change_does_not(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    first = advance(root, brief=brief_path, plan=plan_path, ports=_ports(yt))
    assert first["status"] == "paused"
    searches = yt.searches
    config = root / "config.yaml"
    config.write_text(config.read_text(encoding="utf-8").replace("sleep_s: 0", "sleep_s: 1"), encoding="utf-8")
    second = advance(root, brief=brief_path, plan=plan_path, run_dir=first["run_dir"], ports=_ports(yt))
    assert yt.searches == searches
    config.write_text(config.read_text(encoding="utf-8").replace("max_tiles: 2", "max_tiles: 3"), encoding="utf-8")
    fetches = {"n": 0}
    ports = _ports(yt)

    def fetcher(url):
        fetches["n"] += 1
        return _jpeg()

    ports.rank_fetcher = fetcher
    third = advance(root, brief=brief_path, plan=plan_path, run_dir=first["run_dir"], ports=ports)
    assert yt.searches == searches
    assert fetches["n"] > 0
    assert third["status"] == "paused"


def test_missing_tile_judgments_pause_and_resume(tmp_path, monkeypatch):
    guard = Guard()
    guard.install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / "acq"
    yt.base.mkdir()
    first = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        vision_agree=True,
        ports=_ports(yt),
    )
    assert first["status"] == "paused"
    assert first["stage"] == "label_tiles"
    assert first["missing"] == "tile judgments"
    assert "tile_scores" in first["how_to_supply"]
    assert "--judgments" in first["how_to_supply"]
    assert guard.tripped == []
    state = json.loads((Path(first["run_dir"]) / "runner_state.json").read_text(encoding="utf-8"))
    assert state["approvals"]["vision"] == {"backend": "codex-login", "model": "gpt-6-sol"}
    _patch_media(monkeypatch, yt)
    second = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        run_dir=first["run_dir"],
        ports=_ports(yt, tile_caller=_tile_text),
    )
    assert second["stage"] != "agree_vision"
    assert second["status"] == "paused"
    assert second["stage"] == "label_strips"
    assert guard.tripped == []


class Clock:
    def __init__(self, now: float) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def test_export_allowance_is_the_pause_and_supplying_it_exports(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    monkeypatch.setattr(
        "scenery_brief_clips.jev_gate.gate_run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("jev must not run")),
    )
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / "acq"
    yt.base.mkdir()
    calls = _patch_media(monkeypatch, yt)
    first = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        vision_agree=True,
        ports=_ports(yt, tile_caller=_tile_text, strip_caller=_strip_text),
    )
    assert first["status"] == "paused", first
    assert first["stage"] == "agree_export", first
    assert "export" in (first["missing"] or "")
    assert calls["encode"] == 0
    assert yt.exports == 0
    second = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        run_dir=first["run_dir"],
        vision_agree=True,
        allow_export=True,
        theme="contract-theme",
        ports=_ports(yt, tile_caller=_tile_text, strip_caller=_strip_text),
    )
    assert second["status"] == "completed", second
    assert calls["encode"] == 1
    assert yt.exports == 1


def test_pause_records_wait_time_and_recovery_counts_a_retry(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    clock = Clock(10.0)
    yt = FakeYt()
    paused = advance(root, brief=brief_path, plan=plan_path, ports=_ports(yt, clock=clock))
    assert paused["status"] == "paused"
    assert any(item["status"] == "paused" for item in paused["timing"]["stages"])
    clock.now = 25.0
    advance(
        root,
        brief=brief_path,
        plan=plan_path,
        run_dir=paused["run_dir"],
        vision_agree=True,
        ports=_ports(yt, clock=clock),
    )
    state = json.loads((Path(paused["run_dir"]) / "runner_state.json").read_text(encoding="utf-8"))
    assert state["timing"]["waiting_for_input_s"] > 0

    run_dir = root / "data" / "runs" / "retry"
    run_dir.mkdir(parents=True)
    (run_dir / "runner_inflight.json").write_text(
        json.dumps({"stage": "discover", "binding": "x"}),
        encoding="utf-8",
    )
    recovered = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        run_dir=run_dir,
        acknowledge_uncertain="discover",
        ports=_ports(FakeYt()),
    )
    assert recovered["timing"]["retries"] == 1
    assert not any(
        item["stage"] == "discover" and item["status"] == "reused"
        for item in recovered["timing"]["stages"]
    )


def test_rejected_strip_is_not_exported_and_shortfall_stays(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / "acq"
    yt.base.mkdir()
    calls = _patch_media(monkeypatch, yt)

    def reject_strip(wire, path, prompt):
        return json.dumps(
            {
                "match": "reject",
                "geo": "uncertain",
                "scene_type": "town",
                "note": "a town fills the frame",
                "continuity_ok": False,
            }
        )

    result = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        vision_agree=True,
        allow_export=True,
        theme="contract-theme",
        ports=_ports(yt, tile_caller=_tile_text, strip_caller=reject_strip),
    )
    assert calls["encode"] == 0
    assert yt.exports == 0
    shortlist = json.loads((Path(result["run_dir"]) / "shortlist.json").read_text(encoding="utf-8"))
    assert shortlist["counts"]["n_selected"] == 0
    assert shortlist["shortfall"]["count"] == 3
    assert shortlist["counts"]["request_fulfilled"] is False
    manifest = json.loads((root / "out" / "contract-theme" / "manifest.json").read_text(encoding="utf-8"))
    exported = manifest.get("clips") or manifest.get("exported") or []
    blob = json.dumps(manifest)
    assert "abcdefghijk" not in blob or manifest.get("counts", {}).get("exported", 0) == 0
    assert not list((root / "out" / "contract-theme" / "clips").glob("*.mp4"))


def test_missing_discovery_file_is_not_reused(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    first = advance(root, brief=brief_path, plan=plan_path, ports=_ports(yt))
    assert first["status"] == "paused"
    searches = yt.searches
    run_dir = Path(first["run_dir"])
    (run_dir / "discovery.json").unlink()
    second = advance(root, brief=brief_path, plan=plan_path, run_dir=run_dir, ports=_ports(yt))
    assert yt.searches > searches
    assert not any(
        item["stage"] == "discover" and item["status"] == "reused"
        for item in second["timing"]["stages"]
    )





def test_unreported_label_usage_stays_null_and_counts_the_call(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief_path, plan_path = _brief_plan(root)
    yt = FakeYt()
    clock = Clock(1.0)
    first = advance(root, brief=brief_path, plan=plan_path, vision_agree=True, ports=_ports(yt, clock=clock))
    assert first["status"] == "paused"
    assert first["stage"] == "label_tiles"

    def bare_caller(wire, path, prompt):
        bare_caller.calls += 1
        return json.dumps({"label": "keep", "look": "europe_like", "note": "field"})

    bare_caller.calls = 0
    clock.now = 20.0
    second = advance(
        root,
        brief=brief_path,
        plan=plan_path,
        run_dir=first["run_dir"],
        ports=_ports(yt, tile_caller=bare_caller, clock=clock),
    )
    executed = [item for item in second["timing"]["stages"] if item["stage"] == "label_tiles" and item["status"] == "executed"]
    assert executed, second
    assert executed[-1]["model_calls"] == bare_caller.calls
    assert executed[-1]["model_calls"] > 0
    assert executed[-1]["tokens"] is None
    state = json.loads((Path(first["run_dir"]) / "runner_state.json").read_text(encoding="utf-8"))
    expected = round(sum(item["elapsed_s"] for item in state["timing"]["stages"] if item["status"] in {"executed", "failed"}), 6)
    assert state["active_execution_s"] == expected
    assert isinstance(state["active_execution_s"], float)
