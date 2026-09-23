"""Continuity gate: soft-dissolve / mid-excerpt cut trim-or-reject."""

from pathlib import Path

import pytest

from scenery_brief_clips.analyze import Excerpt
from scenery_brief_clips.continuity import (
    ContinuitySettings,
    FrameFeatures,
    continuity_sample_times,
    decide_from_samples,
    gate_excerpt,
    longest_stable_span,
    sample_dhashes_from_video,
    window_distances,
)
from scenery_brief_clips.shortlist import hamming64

FIXTURES = Path(__file__).parent / "fixtures"


def _feat(dhash: int, rgb: tuple[float, float, float] = (10.0, 10.0, 10.0)) -> FrameFeatures:
    return FrameFeatures(dhash=dhash, mean_rgb=rgb)


def _features_with_cut(
    n: int = 13,
    cut_after: int = 10,
    base: int = 0,
    other: int = 0xFFFFFFFFFFFFFFFF,
) -> list[FrameFeatures]:
    return [
        _feat(base if i <= cut_after else other, (10, 10, 10) if i <= cut_after else (200, 10, 10))
        for i in range(n)
    ]


def test_continuity_sample_times_include_near_endpoints():
    times = continuity_sample_times(10.0, 16.0, fps=2.0)
    assert times[0] == pytest.approx(10.05)
    assert times[-1] == pytest.approx(15.95)
    assert len(times) >= 2


def test_window_distances_detect_adjacent_and_endpoint_jumps():
    features = _features_with_cut(n=5, cut_after=2)
    max_adj_h, end_h, max_adj_c, end_c = window_distances(features)
    assert max_adj_h == 64
    assert end_h == 64
    assert max_adj_c >= 100
    assert end_c >= 100


def test_longest_stable_span_prefers_earlier_equal_length():
    times = [float(i) for i in range(10)]
    features = _features_with_cut(n=10, cut_after=4)
    span = longest_stable_span(
        times,
        features,
        max_adjacent_hamming=18,
        max_endpoint_hamming=22,
        max_adjacent_color=28.0,
        max_endpoint_color=35.0,
    )
    assert span == (0, 4)


def test_decide_keeps_stable_window():
    times = [0.0, 1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    features = [_feat(0) for _ in times]
    decision = decide_from_samples(
        start_s=0.0,
        end_s=6.0,
        times=times,
        features=features,
        target_s=6.0,
        min_s=4.0,
        max_s=12.0,
        max_adjacent_hamming=18,
        max_endpoint_hamming=22,
        max_adjacent_color=28.0,
        max_endpoint_color=35.0,
    )
    assert decision.action == "keep"
    assert (decision.start_s, decision.end_s) == (0.0, 6.0)


def test_decide_trims_to_stable_prefix_before_late_cut():
    times = [i * 0.5 for i in range(13)]  # 0..6 inclusive
    features = _features_with_cut(n=13, cut_after=10)  # stable 0..5.0s
    decision = decide_from_samples(
        start_s=0.0,
        end_s=6.0,
        times=times,
        features=features,
        target_s=6.0,
        min_s=4.0,
        max_s=12.0,
        max_adjacent_hamming=18,
        max_endpoint_hamming=22,
        max_adjacent_color=28.0,
        max_endpoint_color=35.0,
    )
    assert decision.action == "trim"
    assert decision.start_s == 0.0
    assert decision.end_s == pytest.approx(5.0)
    assert decision.original_end_s == 6.0


def test_decide_rejects_when_no_stable_subspan_meets_min():
    times = [float(i) for i in range(7)]
    features = [
        _feat(0 if i % 2 == 0 else 0xFFFFFFFFFFFFFFFF, (10, 10, 10) if i % 2 == 0 else (200, 10, 10))
        for i in range(7)
    ]
    decision = decide_from_samples(
        start_s=0.0,
        end_s=6.0,
        times=times,
        features=features,
        target_s=6.0,
        min_s=4.0,
        max_s=12.0,
        max_adjacent_hamming=18,
        max_endpoint_hamming=22,
        max_adjacent_color=28.0,
        max_endpoint_color=35.0,
    )
    assert decision.action == "reject"
    assert "no stable" in (decision.reason or "") or "below duration_min_s" in (decision.reason or "")


def test_gate_excerpt_on_stable_fixture_keeps():
    path = FIXTURES / "continuity_stable.mp4"
    excerpt = Excerpt(0.5, 6.5, (0.0, 8.0))
    decision = gate_excerpt(
        excerpt,
        video_path=path,
        analysis_span=(0.0, 8.0),
        target_s=6.0,
        min_s=4.0,
        max_s=12.0,
        settings=ContinuitySettings(),
    )
    assert decision.action == "keep"
    assert decision.start_s == 0.5
    assert decision.end_s == 6.5


def test_gate_excerpt_on_hard_cut_fixture_trims_or_rejects():
    path = FIXTURES / "continuity_hard_cut.mp4"  # pattern A 0-5, pattern B 5-10
    excerpt = Excerpt(0.5, 9.5, (0.0, 10.0))
    decision = gate_excerpt(
        excerpt,
        video_path=path,
        analysis_span=(0.0, 10.0),
        target_s=6.0,
        min_s=4.0,
        max_s=12.0,
        settings=ContinuitySettings(),
    )
    assert decision.action in {"trim", "reject"}
    if decision.action == "trim":
        assert decision.end_s - decision.start_s >= 4.0
        assert decision.end_s <= 5.3 or decision.start_s >= 4.7


def test_gate_excerpt_late_cut_trims_away_tail():
    path = FIXTURES / "continuity_late_cut.mp4"  # pattern 0-4, bars 4-8
    excerpt = Excerpt(0.0, 8.0, (0.0, 8.0))
    decision = gate_excerpt(
        excerpt,
        video_path=path,
        analysis_span=(0.0, 8.0),
        target_s=6.0,
        min_s=3.5,
        max_s=12.0,
        settings=ContinuitySettings(),
    )
    assert decision.action == "trim"
    assert decision.end_s - decision.start_s >= 3.5
    # Either stable side is fine; must not straddle the 4s cut.
    assert decision.end_s <= 4.3 or decision.start_s >= 3.7
    assert not (decision.start_s < 3.5 and decision.end_s > 4.5)


def test_sample_dhashes_from_video_returns_matching_counts():
    path = FIXTURES / "continuity_stable.mp4"
    times, hashes = sample_dhashes_from_video(path, 0.0, 6.0, fps=2.0)
    assert len(times) == len(hashes) >= 2
    assert times[0] == pytest.approx(0.05)
    assert times[-1] == pytest.approx(5.95)
    assert hamming64(hashes[0], hashes[-1]) <= 18


def test_analyze_run_continuity_gate_rejects_cut_window(tmp_path):
    import json

    from scenery_brief_clips.pipeline_analyze import analyze_run

    src = FIXTURES / "continuity_hard_cut.mp4"
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    cache_dir = tmp_path / "analysis"
    cache_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps(
            [
                {
                    "video_id": "abcdefghijk",
                    "priority": "promising",
                    "windows": [],
                    "duration_s": 10.0,
                }
            ]
        )
    )
    (run_dir / "constraint.json").write_text(
        json.dumps(
            {"target_duration_s": 6.0, "duration_min_s": 4.0, "duration_max_s": 12.0}
        )
    )

    def fetch(video_id, dest, span):
        dest = Path(dest)
        dest.write_bytes(src.read_bytes())
        return dest

    def detect(path, min_scene_len_s=0.5):
        # Pretend PySceneDetect saw one scene (the soft-dissolve failure mode).
        return [(0.0, 10.0)]

    rows = analyze_run(
        run_dir,
        cache_dir=cache_dir,
        fetch_span=fetch,
        detect_fn=detect,
        max_videos=1,
        continuity_settings=ContinuitySettings(),
    )
    assert len(rows) == 1
    assert rows[0]["status"] == "complete"
    excerpts = rows[0]["excerpts"]
    # Mid-scene cut at 5s must not survive as a straddling candidate.
    for excerpt in excerpts:
        assert excerpt["end_s"] - excerpt["start_s"] >= 4.0
        assert excerpt["continuity"]["action"] in {"keep", "trim"}
        if excerpt["start_s"] < 4.5:
            assert excerpt["end_s"] <= 5.3
        if excerpt["end_s"] > 5.5:
            assert excerpt["start_s"] >= 4.7
    if not excerpts:
        assert rows[0]["continuity_rejected"]
