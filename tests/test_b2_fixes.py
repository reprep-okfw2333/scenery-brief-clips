"""Tests for two fixes: minimum analysis window length, and spans with no video."""

import json
import subprocess
from pathlib import Path

import pytest

from scenery_brief_clips import yt
from scenery_brief_clips.analysis_cache import NO_VIDEO_IN_SPAN
from scenery_brief_clips.analyze import analysis_plan, min_window_for_durations
from scenery_brief_clips.continuity import ContinuitySettings
from scenery_brief_clips.pipeline_analyze import analyze_run
from scenery_brief_clips.verify import (
    _expected_rows,
    _unavailable_problem,
    _validate_settings,
    verify_run,
)
from scenery_brief_clips.yt import AnalysisSpanEmpty, ExportMediaError

from test_verify import _probe, _refresh_manifest, _valid_run, _write


def _win(start, end):
    return {"start_s": float(start), "end_s": float(end)}


def _lengths(plan):
    return [end - start for start, end in plan.ranges]


# ---------------------------------------------------------------- Fix 1: math


@pytest.mark.parametrize(
    "min_s,target_s,pad_s,expected",
    [(4, 6, 2, 8.0), (16, 20, 2, 20.0), (2, 3, 2, 6.0), (10, 25, 2, 25.0)],
)
def test_min_window_for_durations(min_s, target_s, pad_s, expected):
    assert min_window_for_durations(min_s, target_s, pad_s) == pytest.approx(expected)


@pytest.mark.parametrize(
    "duration_s,windows,budget",
    [
        (500.0, [_win(100, 110)], 600.0),
        (500.0, [_win(100, 110), _win(300, 305)], 600.0),
        (3600.0, [_win(100 + i * 400, 109 + i * 400) for i in range(8)], 90.0),
        (3600.0, [_win(100, 140), _win(900, 940), _win(2000, 2040), _win(3000, 3040)], 90.0),
        (3600.0, [], 90.0),
        (3600.0, [], 600.0),
        (300.0, [], 600.0),
        (300.0, [], 90.0),
        (12.0, [_win(3, 5)], 90.0),
    ],
)
def test_min_window_zero_reproduces_old_plans(duration_s, windows, budget):
    omitted = analysis_plan(duration_s, windows, max_analysis_s=budget)
    zero = analysis_plan(duration_s, windows, max_analysis_s=budget, min_window_s=0)
    assert zero == omitted
    assert zero.mode == omitted.mode and zero.ranges == omitted.ranges


def test_plan_modes_covered_by_the_zero_equivalence_cases():
    modes = {
        analysis_plan(500.0, [_win(100, 110)], 600.0).mode,
        analysis_plan(3600.0, [_win(100 + i * 400, 109 + i * 400) for i in range(8)], 90.0).mode,
        analysis_plan(3600.0, [], 90.0).mode,
        analysis_plan(300.0, [], 600.0).mode,
    }
    assert modes == {"windows", "windows_capped", "spread", "full"}


def test_short_window_is_widened_around_its_centre():
    # 9 s padded tile window (7..16), centre 11.5 -> exactly 20 s around it
    plan = analysis_plan(300.0, [_win(9, 14)], max_analysis_s=600.0, pad_s=2.0, min_window_s=20.0)
    assert plan.ranges == [(pytest.approx(1.5), pytest.approx(21.5))]
    (start, end), = plan.ranges
    assert end - start == pytest.approx(20.0)
    assert (start + end) / 2 == pytest.approx(11.5)


def test_widened_window_centred_mid_video():
    plan = analysis_plan(300.0, [_win(100, 105)], max_analysis_s=600.0, pad_s=2.0, min_window_s=20.0)
    (start, end), = plan.ranges
    assert (end - start) == pytest.approx(20.0)
    assert (start + end) / 2 == pytest.approx(102.5)


def test_widened_window_near_start_shifts_to_zero():
    plan = analysis_plan(300.0, [_win(0, 5)], max_analysis_s=600.0, pad_s=2.0, min_window_s=20.0)
    assert plan.ranges == [(0.0, pytest.approx(20.0))]


def test_widened_window_near_end_ends_at_duration():
    plan = analysis_plan(300.0, [_win(295, 299)], max_analysis_s=600.0, pad_s=2.0, min_window_s=20.0)
    (start, end), = plan.ranges
    assert end == pytest.approx(300.0)
    assert end - start == pytest.approx(20.0)


def test_video_shorter_than_min_window_yields_whole_video():
    plan = analysis_plan(12.0, [_win(3, 5)], max_analysis_s=600.0, pad_s=2.0, min_window_s=20.0)
    assert plan.ranges == [(0.0, 12.0)]


def test_overlapping_widened_windows_merge():
    # 1000-1005 and 1015-1020 are two separate padded windows; widened to 20 s
    # they overlap and become one range.
    plan = analysis_plan(
        3600.0, [_win(1000, 1005), _win(1015, 1020)], max_analysis_s=600.0, pad_s=2.0, min_window_s=20.0
    )
    assert len(plan.ranges) == 1
    start, end = plan.ranges[0]
    assert start <= 1002.5 and end >= 1017.5
    # without a minimum they stay separate
    assert len(analysis_plan(3600.0, [_win(1000, 1005), _win(1015, 1020)], 600.0, 2.0).ranges) == 2


def test_windows_capped_with_min_keeps_few_long_spread_ranges():
    windows = [_win(100 + i * 400, 109 + i * 400) for i in range(8)]
    plan = analysis_plan(3600.0, windows, max_analysis_s=90.0, pad_s=2.0, min_window_s=20.0)
    assert plan.mode == "windows_capped"
    assert 1 <= len(plan.ranges) <= 4
    assert all(length >= 20.0 - 1e-6 for length in _lengths(plan))
    assert sum(_lengths(plan)) <= 90.0 + 1e-6
    first, last = plan.ranges[0], plan.ranges[-1]
    assert first[0] <= 111.0 and first[1] >= 98.0  # overlaps the first original window
    assert last[0] <= 2911.0 and last[1] >= 2898.0  # overlaps the last original window
    # the kept ranges are spread across the video, not clustered
    assert last[0] - first[1] > 1500.0


def test_windows_capped_that_fits_min_times_count_still_respects_min():
    windows = [_win(100, 140), _win(900, 940), _win(2000, 2040), _win(3000, 3040)]
    plan = analysis_plan(3600.0, windows, max_analysis_s=90.0, pad_s=2.0, min_window_s=20.0)
    assert plan.mode == "windows_capped"
    assert len(plan.ranges) == 4
    assert all(length >= 20.0 - 1e-6 for length in _lengths(plan))
    assert sum(_lengths(plan)) <= 90.0 + 1e-6


def test_spread_with_min_window_keeps_ranges_long_enough():
    plan = analysis_plan(3600.0, [], max_analysis_s=90.0, pad_s=2.0, min_window_s=20.0)
    assert plan.mode == "spread"
    assert 1 <= len(plan.ranges) <= 4
    assert all(length >= 20.0 - 1e-6 for length in _lengths(plan))
    assert sum(_lengths(plan)) <= 90.0 + 1e-6


def test_spread_with_smaller_min_window():
    plan = analysis_plan(3600.0, [], max_analysis_s=90.0, pad_s=2.0, min_window_s=16.0)
    assert all(length >= 16.0 - 1e-6 for length in _lengths(plan))
    assert sum(_lengths(plan)) <= 90.0 + 1e-6


def test_spread_without_min_window_keeps_old_count():
    plan = analysis_plan(3600.0, [], max_analysis_s=90.0, pad_s=2.0, min_window_s=0.0)
    assert plan.mode == "spread"
    assert len(plan.ranges) == 6


def test_r11_brief_band_16_20_24_fits_a_16s_clip():
    # constraint 16/20/24: two kept tile windows of ~5 s each
    min_s, target_s = 16.0, 20.0
    min_window = min_window_for_durations(min_s, target_s, 2.0)
    assert min_window == pytest.approx(20.0)
    windows = [_win(100, 105), _win(150, 155)]
    plan = analysis_plan(600.0, windows, max_analysis_s=90.0, pad_s=2.0, min_window_s=min_window)
    assert plan.ranges
    assert all(length >= 20.0 - 1e-6 for length in _lengths(plan))
    assert all(length >= min_s for length in _lengths(plan))
    # the old plan had ranges (9 s) too short for a 16 s clip
    old = analysis_plan(600.0, windows, max_analysis_s=90.0, pad_s=2.0)
    assert any(length < min_s for length in _lengths(old))


# -------------------------------------------------------------- Fix 1: verify


def _ranked_for_verify():
    return [
        {
            "video_id": "aaaaaaaaaaa",
            "priority": "promising",
            "duration_s": 300.0,
            "windows": [_win(100, 105)],
        }
    ]


def _verify_settings(**extra):
    settings = {"max_videos": 1, "max_analysis_s": 600.0, "pad_s": 2.0}
    settings.update(extra)
    return settings


def test_expected_rows_without_min_window_setting_uses_old_plan():
    errors: list[str] = []
    rows = _expected_rows(_ranked_for_verify(), _verify_settings(), errors)
    assert errors == []
    assert rows["aaaaaaaaaaa"]["ranges"] == [(98.0, 107.0)]


def test_expected_rows_with_min_window_setting_uses_widened_plan():
    errors: list[str] = []
    rows = _expected_rows(_ranked_for_verify(), _verify_settings(min_window_s=20.0), errors)
    assert errors == []
    (start, end), = rows["aaaaaaaaaaa"]["ranges"]
    assert end - start == pytest.approx(20.0)
    assert (start + end) / 2 == pytest.approx(102.5)
    zero = _expected_rows(_ranked_for_verify(), _verify_settings(min_window_s=0), [])
    assert zero["aaaaaaaaaaa"]["ranges"] == [(98.0, 107.0)]


def _settings_errors(tmp_path, **override):
    run_dir, _ = _valid_run(tmp_path)
    manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    manifest["settings"].update(override)
    errors: list[str] = []
    _validate_settings(manifest, errors)
    return errors


@pytest.mark.parametrize("value", [-1, "x", None, float("nan")])
def test_validate_settings_rejects_invalid_min_window(tmp_path, value):
    errors = _settings_errors(tmp_path, min_window_s=value)
    assert any("min_window_s" in error for error in errors)


@pytest.mark.parametrize("value", [0, 20, 8.5])
def test_validate_settings_accepts_valid_min_window(tmp_path, value):
    assert _settings_errors(tmp_path, min_window_s=value) == []


def test_validate_settings_accepts_missing_min_window(tmp_path):
    assert _settings_errors(tmp_path) == []


# ------------------------------------------------ Fix 2: yt no_streams probing


def _fake_ffprobe(monkeypatch, streams):
    calls = []

    def fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(
            cmd, 0, stdout=json.dumps({"streams": streams, "format": {}}), stderr=""
        )

    monkeypatch.setattr(yt.subprocess, "run", fake_run)
    return calls


def test_probe_export_coverage_no_streams(monkeypatch, tmp_path):
    _fake_ffprobe(monkeypatch, [])
    with pytest.raises(ExportMediaError) as info:
        yt.probe_export_coverage(tmp_path / "x.mp4")
    assert info.value.code == "no_streams"


def test_probe_export_coverage_missing_streams_key_is_no_streams(monkeypatch, tmp_path):
    monkeypatch.setattr(
        yt.subprocess,
        "run",
        lambda cmd, **kw: subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr=""),
    )
    with pytest.raises(ExportMediaError) as info:
        yt.probe_export_coverage(tmp_path / "x.mp4")
    assert info.value.code == "no_streams"


def test_probe_export_coverage_audio_only_is_streams_invalid(monkeypatch, tmp_path):
    _fake_ffprobe(monkeypatch, [{"codec_type": "audio", "codec_name": "aac"}])
    with pytest.raises(ExportMediaError) as info:
        yt.probe_export_coverage(tmp_path / "x.mp4")
    assert info.value.code == "streams_invalid"


def test_validate_analysis_media_turns_no_streams_into_span_empty(monkeypatch, tmp_path):
    def probe(_path):
        raise ExportMediaError("no_streams", "nothing here")

    monkeypatch.setattr(yt, "probe_export_coverage", probe)
    with pytest.raises(AnalysisSpanEmpty) as info:
        yt.validate_analysis_media(tmp_path / "x.mp4", (10.0, 20.0))
    assert "no_streams" in str(info.value)
    assert isinstance(info.value, RuntimeError)


def test_validate_analysis_media_other_probe_errors_stay_plain(monkeypatch, tmp_path):
    def probe(_path):
        raise ExportMediaError("streams_invalid", "audio")

    monkeypatch.setattr(yt, "probe_export_coverage", probe)
    with pytest.raises(RuntimeError) as info:
        yt.validate_analysis_media(tmp_path / "x.mp4", (10.0, 20.0))
    assert not isinstance(info.value, AnalysisSpanEmpty)
    assert "streams_invalid" in str(info.value)


# ------------------------------------------------- Fix 2: analyzer behaviour


def _analysis_run(tmp_path, windows, duration_s=500.0):
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    (run_dir / "ranked.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "abcdefghijk",
                    "title": "Alps",
                    "priority": "promising",
                    "windows": windows,
                    "duration_s": duration_s,
                }
            ]
        )
    )
    (run_dir / "constraint.json").write_text(
        json.dumps({"target_duration_s": 6.0, "duration_min_s": 4.0, "duration_max_s": 12.0})
    )
    return run_dir


def _run(tmp_path, run_dir, fetch, attempts=2):
    return analyze_run(
        run_dir,
        cache_dir=tmp_path / "analysis",
        fetch_span=fetch,
        detect_fn=lambda path, min_scene_len_s: [(0.0, 8.0)],
        max_videos=1,
        acquisition_attempts=attempts,
        continuity_settings=ContinuitySettings(enabled=False),
    )


def _empty_error():
    return AnalysisSpanEmpty("analysis media no_streams: acquisition contains no streams")


def test_empty_span_is_unavailable_and_row_stays_complete(tmp_path):
    run_dir = _analysis_run(tmp_path, [_win(100, 110), _win(200, 210)])
    attempts = []

    def fetch(_vid, dest, span):
        attempts.append(span)
        if span[0] > 150:
            raise _empty_error()
        Path(dest).write_bytes(b"video")
        return Path(dest)

    rows = _run(tmp_path, run_dir, fetch, attempts=2)
    row = rows[0]
    assert [r["status"] for r in row["ranges"]] == ["complete", "unavailable"]
    unavailable = row["ranges"][1]
    assert unavailable["reason"] == NO_VIDEO_IN_SPAN == "no_video_in_span"
    assert unavailable["stage"] == "acquire"
    assert unavailable["attempts"] == 2
    assert len(unavailable["attempt_errors"]) == 2
    assert all("no_streams" in e["error"] for e in unavailable["attempt_errors"])
    assert row["status"] == "complete"
    assert row["errors"] == []
    assert row["excerpts"]
    assert len(row["copies"]) == 1
    assert row["ready_for_shortlist"] is True
    manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    assert manifest["settings"]["min_window_s"] == pytest.approx(8.0)


def test_every_span_unavailable_is_complete_without_excerpts(tmp_path):
    run_dir = _analysis_run(tmp_path, [_win(100, 110), _win(200, 210)])

    def fetch(*_args):
        raise _empty_error()

    row = _run(tmp_path, run_dir, fetch, attempts=2)[0]
    assert [r["status"] for r in row["ranges"]] == ["unavailable", "unavailable"]
    assert row["status"] == "complete"
    assert row["errors"] == []
    assert row["excerpts"] == []
    assert row["copies"] == []
    assert row["ready_for_shortlist"] is False


def test_single_attempt_unavailable_records_one_attempt(tmp_path):
    run_dir = _analysis_run(tmp_path, [_win(100, 110)])

    def fetch(*_args):
        raise _empty_error()

    row = _run(tmp_path, run_dir, fetch, attempts=1)[0]
    assert row["ranges"][0]["status"] == "unavailable"
    assert row["ranges"][0]["attempts"] == 1


def test_mixed_empty_then_plain_failure_is_failed(tmp_path):
    run_dir = _analysis_run(tmp_path, [_win(100, 110)])
    calls = []

    def fetch(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise _empty_error()
        raise RuntimeError("HTTP 403")

    row = _run(tmp_path, run_dir, fetch, attempts=2)[0]
    assert len(calls) == 2
    assert row["ranges"][0]["status"] == "failed"
    assert "reason" not in row["ranges"][0]
    assert row["status"] == "failed"
    assert len(row["errors"]) == 1


def test_mixed_plain_then_empty_failure_is_failed(tmp_path):
    run_dir = _analysis_run(tmp_path, [_win(100, 110)])
    calls = []

    def fetch(*_args):
        calls.append(1)
        if len(calls) == 1:
            raise RuntimeError("HTTP 403")
        raise _empty_error()

    row = _run(tmp_path, run_dir, fetch, attempts=2)[0]
    assert row["ranges"][0]["status"] == "failed"
    assert row["status"] == "failed"


def test_plain_failures_stay_failed_and_partial(tmp_path):
    run_dir = _analysis_run(tmp_path, [_win(100, 110), _win(200, 210)])

    def fetch(_vid, dest, span):
        if span[0] > 150:
            raise RuntimeError("HTTP 403")
        Path(dest).write_bytes(b"video")
        return Path(dest)

    row = _run(tmp_path, run_dir, fetch, attempts=2)[0]
    assert [r["status"] for r in row["ranges"]] == ["complete", "failed"]
    assert row["status"] == "partial"
    assert len(row["errors"]) == 1
    assert row["ready_for_shortlist"] is False

    def always_fail(*_args):
        raise RuntimeError("down")

    row = _run(tmp_path / "second", _analysis_run(tmp_path / "second", [_win(100, 110)]), always_fail)[0]
    assert row["ranges"][0]["status"] == "failed"
    assert row["status"] == "failed"


def test_unavailable_next_to_failed_range_makes_row_partial_or_failed(tmp_path):
    run_dir = _analysis_run(tmp_path, [_win(100, 110), _win(200, 210)])

    def fetch(_vid, dest, span):
        if span[0] > 150:
            raise _empty_error()
        raise RuntimeError("HTTP 500")

    row = _run(tmp_path, run_dir, fetch, attempts=2)[0]
    assert [r["status"] for r in row["ranges"]] == ["failed", "unavailable"]
    assert row["status"] == "failed"
    assert len(row["errors"]) == 1


# ------------------------------------------ Fix 2: verify._unavailable_problem


def _attempt_errors(n=2, error="analysis media no_streams: acquisition contains no streams"):
    return [{"attempt": i + 1, "stage": "acquire", "error": error} for i in range(n)]


def _outcome(**override):
    outcome = {
        "span": [10.0, 20.0],
        "status": "unavailable",
        "reason": NO_VIDEO_IN_SPAN,
        "stage": "acquire",
        "attempts": 2,
        "attempt_errors": _attempt_errors(2),
        "error": "analysis media no_streams: x",
    }
    outcome.update(override)
    return outcome


SETTINGS = {"acquisition_attempts": 2}


def test_unavailable_problem_accepts_exact_form():
    assert _unavailable_problem(_outcome(), SETTINGS) is None


@pytest.mark.parametrize(
    "outcome,settings",
    [
        (_outcome(reason="other"), SETTINGS),
        (_outcome(reason=None), SETTINGS),
        (_outcome(stage="detect"), SETTINGS),
        (_outcome(attempts=1, attempt_errors=_attempt_errors(1)), SETTINGS),
        (_outcome(attempt_errors=[]), SETTINGS),
        (_outcome(attempt_errors=None), SETTINGS),
        (_outcome(attempt_errors=_attempt_errors(2, error="HTTP 403")), SETTINGS),
        (
            _outcome(attempt_errors=[_attempt_errors(1)[0], {"attempt": 2, "stage": "acquire", "error": "boom"}]),
            SETTINGS,
        ),
        (
            _outcome(attempt_errors=[_attempt_errors(1)[0], {"attempt": 2, "stage": "detect", "error": "no_streams"}]),
            SETTINGS,
        ),
        (_outcome(attempts=3), SETTINGS),
        (_outcome(), {"acquisition_attempts": 3}),
        (_outcome(), {}),
        (_outcome(), None),
        (_outcome(), {"acquisition_attempts": "many"}),
    ],
)
def test_unavailable_problem_rejects_everything_else(outcome, settings):
    problem = _unavailable_problem(outcome, settings)
    assert isinstance(problem, str) and problem


# ------------------------------------------------ Fix 2: end-to-end verify_run


def _make_unavailable(run_dir, **override):
    rows = json.loads((run_dir / "excerpts.json").read_text())
    row = rows[0]
    media_name = row["copies"][0]["cache_key"]
    row["ranges"] = [
        {
            "span": [10.0, 20.0],
            "cache_key": media_name,
            "status": "unavailable",
            "reason": NO_VIDEO_IN_SPAN,
            "stage": "acquire",
            "attempts": 2,
            "attempt_errors": _attempt_errors(2),
            "error": "analysis media no_streams: x",
            **override,
        }
    ]
    row["copies"] = []
    row["excerpts"] = []
    row["status"] = "complete"
    row["ready_for_shortlist"] = False
    _write(run_dir / "excerpts.json", rows)
    _refresh_manifest(run_dir)


def test_verify_run_accepts_exact_unavailable_range_with_warning(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    _make_unavailable(run_dir)
    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert report["errors"] == []
    assert report["ok"] is True
    assert any("unavailable" in warning for warning in report["warnings"])


@pytest.mark.parametrize(
    "override",
    [{"reason": "something_else"}, {"stage": "detect"}, {"attempts": 1}],
)
def test_verify_run_rejects_malformed_unavailable_range(tmp_path, override):
    run_dir, _ = _valid_run(tmp_path)
    _make_unavailable(run_dir, **override)
    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert report["ok"] is False
    assert any("marked unavailable" in error for error in report["errors"])
