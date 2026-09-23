import pytest

from scenery_brief_clips.analyze import analysis_plan, excerpts_from_scenes


def test_windows_cheaper_than_full_uses_padded_windows():
    plan = analysis_plan(
        duration_s=3600,
        windows=[{"start_s": 100.0, "end_s": 110.0}, {"start_s": 500.0, "end_s": 510.0}],
        max_analysis_s=600,
        pad_s=2.0,
    )
    assert plan.mode == "windows"
    assert plan.ranges[0] == (98.0, 112.0)
    assert plan.ranges[1] == (498.0, 512.0)
    assert plan.total_s <= 600


def test_overlapping_windows_are_merged():
    plan = analysis_plan(
        duration_s=1000,
        windows=[{"start_s": 10.0, "end_s": 20.0}, {"start_s": 18.0, "end_s": 30.0}],
        max_analysis_s=600,
        pad_s=1.0,
    )
    assert plan.mode == "windows"
    assert plan.ranges == [(9.0, 31.0)]


def test_scored_windows_remain_authoritative_even_when_they_cover_half_of_short_video():
    plan = analysis_plan(
        duration_s=100,
        windows=[{"start_s": 0.0, "end_s": 50.0}],
        max_analysis_s=120,
        pad_s=0.0,
    )
    assert plan.mode == "windows"
    assert plan.ranges == [(0.0, 50.0)]


def test_windows_over_budget_are_capped_inside_each_selected_window():
    windows = [
        {"start_s": 0.0, "end_s": 100.0},
        {"start_s": 400.0, "end_s": 500.0},
    ]
    plan = analysis_plan(
        duration_s=1000,
        windows=windows,
        max_analysis_s=60,
        pad_s=0.0,
    )
    assert plan.mode == "windows_capped"
    assert plan.total_s <= 60 + 1e-6
    assert len(plan.ranges) == 2
    assert 0.0 <= plan.ranges[0][0] < plan.ranges[0][1] <= 100.0
    assert 400.0 <= plan.ranges[1][0] < plan.ranges[1][1] <= 500.0


def test_short_video_without_windows_is_full():
    plan = analysis_plan(duration_s=120, windows=[], max_analysis_s=600, pad_s=2.0)
    assert plan.mode == "full"
    assert plan.ranges == [(0.0, 120.0)]


def test_long_video_without_windows_spreads_budget():
    plan = analysis_plan(duration_s=3600, windows=[], max_analysis_s=120, pad_s=0.0)
    assert plan.mode == "spread"
    assert plan.total_s <= 120 + 1e-6
    assert len(plan.ranges) >= 2
    assert plan.ranges[0][0] == 0.0
    assert plan.ranges[-1][1] <= 3600.0


def test_windows_clamped_to_duration():
    plan = analysis_plan(
        duration_s=50,
        windows=[{"start_s": 0.0, "end_s": 10.0}, {"start_s": 48.0, "end_s": 60.0}],
        max_analysis_s=600,
        pad_s=5.0,
    )
    assert plan.ranges[0][0] == 0.0
    assert plan.ranges[-1][1] == 50.0


def test_short_shot_below_min_duration_is_dropped():
    out = excerpts_from_scenes([(0.0, 2.0)], target_s=6.0, min_s=4.0, max_s=12.0, edge_trim_s=0.3)
    assert out == []


def test_shot_in_band_keeps_clean_interior():
    out = excerpts_from_scenes([(0.0, 10.0)], target_s=6.0, min_s=4.0, max_s=12.0, edge_trim_s=0.5)
    assert len(out) == 1
    assert out[0].start_s == 0.5
    assert out[0].end_s == 9.5
    assert out[0].source_scene == (0.0, 10.0)


def test_long_shot_yields_target_length_excerpt_inside():
    out = excerpts_from_scenes([(0.0, 70.0)], target_s=6.0, min_s=4.0, max_s=12.0, edge_trim_s=0.5)
    assert len(out) == 1
    assert abs((out[0].end_s - out[0].start_s) - 6.0) < 1e-6
    assert out[0].start_s >= 0.5
    assert out[0].end_s <= 69.5


def test_title_card_cut_does_not_hide_following_shot():
    # 0.5s title then 8s landscape: both cuts must survive detection spacing;
    # excerpt logic drops the title and keeps the landscape interior.
    out = excerpts_from_scenes(
        [(0.0, 0.5), (0.5, 8.5)],
        target_s=6.0,
        min_s=4.0,
        max_s=12.0,
        edge_trim_s=0.2,
    )
    assert len(out) == 1
    assert out[0].start_s >= 0.5
    assert out[0].end_s <= 8.5


def test_duration_settings_reject_inconsistent_constraints():
    from scenery_brief_clips.analyze import ConstraintError, duration_settings_from_constraint

    with pytest.raises(ConstraintError):
        duration_settings_from_constraint(
            {"target_duration_s": -5.0, "duration_min_s": 1.0, "duration_max_s": 12.0}
        )
    with pytest.raises(ConstraintError):
        duration_settings_from_constraint(
            {"target_duration_s": 6.0, "duration_min_s": 4.0, "duration_max_s": 5.0}
        )
    with pytest.raises(ConstraintError):
        duration_settings_from_constraint({"duration_min_s": 0.0})
    with pytest.raises(ConstraintError):
        duration_settings_from_constraint({"duration_min_s": 12.0, "duration_max_s": 4.0})
    with pytest.raises(ConstraintError):
        duration_settings_from_constraint({"target_duration_s": "soon"})

    assert duration_settings_from_constraint({}) == (6.0, 4.0, 12.0)
    assert duration_settings_from_constraint(
        {"target_duration_s": 6.0, "duration_min_s": 4.0, "duration_max_s": 12.0}
    ) == (6.0, 4.0, 12.0)


def test_excerpts_from_scenes_rejects_invalid_settings():
    from scenery_brief_clips.analyze import ConstraintError

    with pytest.raises(ConstraintError):
        excerpts_from_scenes([(0.0, 50.0)], target_s=-5.0, min_s=1.0, max_s=12.0)
    with pytest.raises(ConstraintError):
        excerpts_from_scenes([(0.0, 50.0)], target_s=6.0, min_s=4.0, max_s=5.0)

    excerpts = excerpts_from_scenes([(0.0, 50.0)], target_s=6.0, min_s=4.0, max_s=12.0)
    assert len(excerpts) == 1
    assert excerpts[0].end_s > excerpts[0].start_s
