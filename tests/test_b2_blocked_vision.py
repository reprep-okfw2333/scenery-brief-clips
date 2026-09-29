"""YouTube bot-check blocks become runner status "blocked"; vision labeling runs concurrently.

Every yt-dlp, media and model call here is a fake. No network, no key.
"""

from __future__ import annotations

import json
import random
import threading
import time
from pathlib import Path

import pytest
from PIL import Image

from scenery_brief_clips import runner
from scenery_brief_clips.runner import advance
from scenery_brief_clips.vision_wire import (
    VisionWire,
    VisionWireError,
    label_review_strips,
    vision_workers,
)
from scenery_brief_clips.yt import is_youtube_block
from test_runner_contract import (
    FakeYt,
    Guard,
    _brief_plan,
    _patch_media,
    _ports,
    _root,
    _strip_text,
    _tile_text,
)

CURLY = "ERROR: [youtube] abc: Sign in to confirm you’re not a bot. Use --cookies-from-browser or --cookies"
STRAIGHT = "ERROR: [youtube] abc: Sign in to confirm you're not a bot."
WIRE = VisionWire("codex-login", "test-model")


# is_youtube_block -----------------------------------------------------------


@pytest.mark.parametrize(
    "text, expected",
    [
        (CURLY, True),
        (STRAIGHT, True),
        (RuntimeError(CURLY), True),
        ("ERROR: [youtube] abc: Video unavailable", False),
        ("HTTP Error 429: Too Many Requests", False),
        ("Sign in to confirm your age", False),
        (None, False),
        ("", False),
    ],
)
def test_is_youtube_block(text, expected):
    assert is_youtube_block(text) is expected


# runner: blocked vs failed ---------------------------------------------------


def _assert_blocked(result, stage):
    assert result["status"] == "blocked", result
    assert result["stage"] == stage
    assert result["error"]
    assert "YouTube" in result["missing"]
    how = result["how_to_supply"].lower()
    assert "wait" in how and "rerun the same command" in how
    assert "the owner may opt in" in how


def test_discover_search_block_is_blocked(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()

    def search(query, limit):
        raise RuntimeError(CURLY)

    yt.search = search
    result = advance(root, brief=brief, plan=plan, ports=_ports(yt))
    _assert_blocked(result, "discover")
    state = json.loads((Path(result["run_dir"]) / "runner_state.json").read_text())
    assert state["failed_stage"] == "discover"


def test_discover_metadata_block_is_blocked(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()

    def metadata(video_id):
        raise RuntimeError(STRAIGHT)

    yt.fetch_metadata = metadata
    result = advance(root, brief=brief, plan=plan, ports=_ports(yt))
    _assert_blocked(result, "discover")


def test_discover_plain_search_error_stays_failed(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()

    def search(query, limit):
        raise RuntimeError("HTTP Error 500: server exploded")

    yt.search = search
    result = advance(root, brief=brief, plan=plan, ports=_ports(yt))
    assert result["status"] == "failed", result
    assert result["stage"] == "discover"
    assert "how_to_supply" not in result or "wait" not in str(result.get("how_to_supply")).lower()


def test_stage_exception_with_marker_is_blocked(tmp_path, monkeypatch):
    """Any stage raising an exception whose text carries the marker."""
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)

    def boom(*args, **kwargs):
        raise RuntimeError(CURLY)

    monkeypatch.setattr(runner, "_discover", boom)
    result = advance(root, brief=brief, plan=plan, ports=_ports(FakeYt()))
    _assert_blocked(result, "discover")


def test_stage_exception_without_marker_stays_failed(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)

    def boom(*args, **kwargs):
        raise RuntimeError("something else broke")

    monkeypatch.setattr(runner, "_discover", boom)
    result = advance(root, brief=brief, plan=plan, ports=_ports(FakeYt()))
    assert result["status"] == "failed", result
    assert result["stage"] == "discover"


def _analyze_ports(yt, spans, message):
    ports = _ports(yt, tile_caller=_tile_text, strip_caller=_strip_text)
    good_fetch = ports.fetch_span

    def fetch_span(video_id, dest, span, format_id=None):
        spans.append(tuple(span))
        if message[0] is not None:
            raise RuntimeError(message[0])
        return good_fetch(video_id, dest, span, format_id=format_id)

    ports.fetch_span = fetch_span
    return ports


def test_analyze_block_is_blocked_and_rerun_resumes(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / "acq"
    yt.base.mkdir()
    _patch_media(monkeypatch, yt)
    tile_calls = []

    def tile_caller(wire, path, prompt):
        tile_calls.append(path)
        return _tile_text(wire, path, prompt)

    spans: list = []
    message = [CURLY]
    ports = _analyze_ports(yt, spans, message)
    ports.tile_caller = tile_caller

    first = advance(root, brief=brief, plan=plan, ports=ports, vision_agree=True)
    _assert_blocked(first, "analyze")
    assert len(spans) >= 1  # every attempt raised the marker error
    run_dir = first["run_dir"]
    state = json.loads((Path(run_dir) / "runner_state.json").read_text())
    for done in ("discover", "rank", "label_tiles"):
        assert done in state["completed"], state["completed"].keys()
    assert "analyze" not in state["completed"]
    searches, tiles = yt.searches, len(tile_calls)
    assert searches > 0 and tiles > 0

    # Block lifts: same command, same run dir. Completed stages are reused.
    message[0] = None
    second = advance(root, brief=brief, plan=plan, ports=ports, run_dir=run_dir, vision_agree=True)
    assert second["status"] != "blocked", second
    assert second["status"] != "failed", second
    assert yt.searches == searches
    assert len(tile_calls) == tiles
    reused = {item["stage"] for item in second["timing"]["stages"] if item["status"] == "reused"}
    assert {"discover", "label_tiles"} <= reused
    state = json.loads((Path(run_dir) / "runner_state.json").read_text())
    assert "analyze" in state["completed"]


def test_analyze_plain_failure_stays_failed(tmp_path, monkeypatch):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / "acq"
    yt.base.mkdir()
    _patch_media(monkeypatch, yt)
    ports = _analyze_ports(yt, [], ["ERROR: [youtube] abc: Video unavailable"])
    result = advance(root, brief=brief, plan=plan, ports=ports, vision_agree=True)
    assert result["status"] == "failed", result
    assert result["stage"] == "analyze"


def _export_run(tmp_path, monkeypatch, message):
    Guard().install(monkeypatch)
    root = _root(tmp_path)
    brief, plan = _brief_plan(root)
    yt = FakeYt()
    yt.base = root / "acq"
    yt.base.mkdir()
    _patch_media(monkeypatch, yt)

    def fetch_export(spec, timeout=900):
        raise RuntimeError(message)

    yt.fetch_export = fetch_export
    ports = _ports(yt, tile_caller=_tile_text, strip_caller=_strip_text)
    return advance(root, brief=brief, plan=plan, ports=ports, vision_agree=True,
                   allow_export=True, theme="blocked-theme")


def test_export_block_is_blocked(tmp_path, monkeypatch):
    _assert_blocked(_export_run(tmp_path, monkeypatch, CURLY), "export")


def test_export_plain_failure_stays_failed(tmp_path, monkeypatch):
    result = _export_run(tmp_path, monkeypatch, "ERROR: [youtube] abc: Video unavailable")
    assert result["status"] == "failed", result
    assert result["stage"] == "export"


# vision_workers ---------------------------------------------------------------


@pytest.mark.parametrize(
    "value, expected",
    [
        (None, 4),
        ("", 4),
        ("1", 1),
        ("3", 3),
        ("0", 1),
        ("-5", 1),
        ("99", 8),
        ("8", 8),
        ("abc", 4),
        ("2.5", 4),
    ],
)
def test_vision_workers_env(monkeypatch, value, expected):
    if value is None:
        monkeypatch.delenv("SCENERY_VISION_WORKERS", raising=False)
    else:
        monkeypatch.setenv("SCENERY_VISION_WORKERS", value)
    assert vision_workers() == expected


# label_review_strips concurrency ------------------------------------------------


def _strip_reply(note: str, *, ok: bool = True) -> str:
    return json.dumps(
        {"match": "keep", "geo": "uncertain", "scene_type": "field", "note": note, "continuity_ok": ok}
    )


def _review(run_dir: Path, layout, **extra):
    """layout: list of (video_id, excerpt_index). One distinct strip image each."""
    moments = []
    for n, (video_id, index) in enumerate(layout):
        strip = run_dir / f"strip-{n}.jpg"
        Image.new("RGB", (8, 8), (n * 10 % 256, 90, 40 + n)).save(strip)
        moments.append({"video_id": video_id, "excerpt_index": index, "strip": strip.name})
    (run_dir / "review.json").write_text(
        json.dumps({"excerpts_sha256": "abc", "moments": moments, **extra}), encoding="utf-8"
    )
    return moments


LAYOUT = [
    ("vidA", 0), ("vidB", 0), ("vidC", 0), ("vidA", 1), ("vidB", 1), ("vidC", 1),
    ("vidA", 2), ("vidC", 2), ("vidB", 2), ("vidA", 3),
]


class Tracker:
    """Fake vision caller: random short sleep, counts in-flight calls, notes = strip name."""

    def __init__(self, seed=7, fail_names=()):
        self.rng = random.Random(seed)
        self.lock = threading.Lock()
        self.active = 0
        self.peak = 0
        self.calls = 0
        self.fail_names = set(fail_names)

    def __call__(self, wire, path, prompt):
        with self.lock:
            self.active += 1
            self.peak = max(self.peak, self.active)
            self.calls += 1
            delay = self.rng.uniform(0.0, 0.05)
        time.sleep(delay)
        with self.lock:
            self.active -= 1
        name = Path(path).name
        if name in self.fail_names:
            raise VisionWireError(f"boom on {name}")
        return _strip_reply(name, ok=False)


def _run(tmp_path, workers, monkeypatch, layout=LAYOUT, tracker=None, **extra):
    monkeypatch.setenv("SCENERY_VISION_WORKERS", str(workers))
    run_dir = tmp_path / f"run-w{workers}"
    run_dir.mkdir()
    moments = _review(run_dir, layout, **extra)
    tracker = tracker or Tracker()
    return label_review_strips(run_dir, WIRE, tracker), tracker, moments


def test_strip_labels_keep_review_order_and_overlap(tmp_path, monkeypatch):
    serial, serial_tracker, _ = _run(tmp_path, 1, monkeypatch)
    parallel, parallel_tracker, _ = _run(tmp_path, 4, monkeypatch, tracker=Tracker(seed=99))

    assert serial_tracker.peak == 1
    assert parallel_tracker.peak > 1
    assert parallel_tracker.peak <= 4
    assert serial["failures"] == [] and parallel["failures"] == []
    assert serial_tracker.calls == parallel_tracker.calls == len(LAYOUT)

    # Same per-video order (review order) as the serial run, whatever finished first.
    assert parallel["scores"] == serial["scores"]
    for video in ("vidA", "vidB", "vidC"):
        expected = [i for v, i in LAYOUT if v == video]
        assert [e["excerpt_index"] for e in parallel["scores"][video]] == expected
        expected_notes = [f"strip-{n}.jpg" for n, (v, _i) in enumerate(LAYOUT) if v == video]
        assert [e["note"] for e in parallel["scores"][video]] == expected_notes
    assert parallel["scores"]["excerpts_sha256"] == "abc"


def test_strip_failure_is_reported_and_others_are_labeled(tmp_path, monkeypatch):
    tracker = Tracker(fail_names={"strip-3.jpg"})
    result, tracker, _ = _run(tmp_path, 4, monkeypatch, tracker=tracker)
    assert len(result["failures"]) == 1
    assert result["failures"][0]["path"].endswith("strip-3.jpg")
    assert "boom" in result["failures"][0]["error"]
    labeled = sum(len(v) for k, v in result["scores"].items() if k != "excerpts_sha256")
    assert labeled == len(LAYOUT) - 1
    assert [e["excerpt_index"] for e in result["scores"]["vidA"]] == [0, 2, 3]  # index 1 (strip-3) failed


def test_continuity_suspect_note_prefix_under_concurrency(tmp_path, monkeypatch):
    monkeypatch.setenv("SCENERY_VISION_WORKERS", "4")
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    moments = _review(run_dir, LAYOUT[:6])
    suspects = {1, 4}
    for n in suspects:
        moments[n]["continuity_suspect"] = True
    review = json.loads((run_dir / "review.json").read_text())
    review["moments"] = moments
    (run_dir / "review.json").write_text(json.dumps(review))

    def caller(wire, path, prompt):
        time.sleep(0.01)
        # strip-4 is cleared by the model, strip-1 is not
        return _strip_reply(Path(path).name, ok=Path(path).name == "strip-4.jpg")

    result = label_review_strips(run_dir, WIRE, caller)
    notes = {e["note"] for k, v in result["scores"].items() if k != "excerpts_sha256" for e in v}
    prefixed = {n for n in notes if n.startswith("continuity not cleared:")}
    assert prefixed == {"continuity not cleared: strip-1.jpg"}
    # suspect but cleared by the model: no "not cleared" prefix
    assert "continuity_ok: strip-4.jpg" in notes
    assert "strip-0.jpg" in notes  # not suspect: untouched


# _wrap_caller thread safety --------------------------------------------------


def test_wrap_caller_counts_every_call_across_threads():
    counters = runner._Counters()
    assert hasattr(counters.lock, "acquire")
    seen = []

    def caller(wire, path, prompt):
        seen.append(1)
        return "ok"

    caller.last_usage = {"total_tokens": 1}
    wrapped = runner._wrap_caller(caller, counters, "strip")
    barrier = threading.Barrier(16)
    errors = []

    def work():
        try:
            barrier.wait()
            for _ in range(50):
                assert wrapped(None, "img.jpg", "p") == "ok"
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=work) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(seen) == 800
    assert counters.model_calls == 800
    assert len(counters.calls) == 800
    assert set(counters.calls) == {"strip"}
