"""Analyzed-source count scales with the brief's n_clips (runner, 2026-09-29)."""
import json
from pathlib import Path

import pytest

import scenery_brief_clips.runner as runner
from scenery_brief_clips.runner import (
    DEFAULT_RANK_VIDEOS,
    MAX_DERIVED_SOURCES,
    _bindings,
    config_with_brief_source_count,
    sources_for_clips,
)
from test_brief import valid_brief, valid_plan


@pytest.mark.parametrize(
    "n_clips, sources",
    [(1, 2), (2, 3), (3, 3), (4, 4), (5, 5), (6, 5), (8, 7), (10, 8), (15, 11), (20, 12), (500, 12)],
)
def test_sources_for_clips(n_clips, sources):
    assert sources_for_clips(n_clips) == sources


def test_sources_never_exceed_cap():
    assert max(sources_for_clips(n) for n in range(1, 200)) == MAX_DERIVED_SOURCES


def test_silent_config_gets_derived_counts():
    assert config_with_brief_source_count({}, {"n_clips": 2}) == {"max_analyze_videos": 3}
    assert config_with_brief_source_count({}, {"n_clips": 10}) == {
        "max_analyze_videos": 8, "max_rank_videos": 12}


def test_rank_raised_only_above_default():
    for n in range(1, 60):
        derived = config_with_brief_source_count({}, {"n_clips": n})
        analyze = derived["max_analyze_videos"]
        if analyze + runner.RANK_SPARE_VIDEOS > DEFAULT_RANK_VIDEOS:
            assert derived["max_rank_videos"] == analyze + runner.RANK_SPARE_VIDEOS
        else:
            assert "max_rank_videos" not in derived


def test_explicit_config_wins():
    config = {"max_analyze_videos": 5, "max_rank_videos": 5, "max_tiles": 6}
    assert config_with_brief_source_count(config, {"n_clips": 10}) == config
    # max_analyze_videos alone: rank is derived from it, not from n_clips.
    assert config_with_brief_source_count({"max_analyze_videos": 2}, {"n_clips": 20}) == {
        "max_analyze_videos": 2}
    assert config_with_brief_source_count({"max_rank_videos": 3}, {"n_clips": 20}) == {
        "max_rank_videos": 3, "max_analyze_videos": 12}


def test_input_config_not_mutated():
    config = {}
    config_with_brief_source_count(config, {"n_clips": 10})
    assert config == {}


def test_derived_count_enters_rank_and_analyze_bindings():
    brief = valid_brief()
    model = {"backend": "openai-api", "model": "m"}
    silent = _bindings(brief, None, config_with_brief_source_count({}, {"n_clips": 10}), model)
    pinned = _bindings(brief, None, {"max_analyze_videos": 8, "max_rank_videos": 12}, model)
    assert silent["analyze"] == pinned["analyze"]
    assert silent["rank"] == pinned["rank"]
    other = _bindings(brief, None, config_with_brief_source_count({}, {"n_clips": 2}), model)
    assert other["analyze"] != silent["analyze"]
    assert other["discover"] == silent["discover"]


def _root(tmp_path: Path, config_lines: list[str]) -> Path:
    root = tmp_path / "proj"
    root.mkdir()
    (root / "vision.yaml").write_text("backend: codex-login\nmodel: gpt-6-sol\n", encoding="utf-8")
    (root / "config.yaml").write_text("\n".join(config_lines) + "\n", encoding="utf-8")
    (root / "tmp").mkdir()
    return root


class _Stop(Exception):
    pass


@pytest.mark.parametrize(
    "config_lines, n_clips, expected",
    [
        (["sleep_s: 0"], 10, {"max_analyze_videos": 8, "max_rank_videos": 12}),
        (["sleep_s: 0"], 2, {"max_analyze_videos": 3, "max_rank_videos": None}),
        (["sleep_s: 0", "max_analyze_videos: 5", "max_rank_videos: 5"], 10,
         {"max_analyze_videos": 5, "max_rank_videos": 5}),
    ],
)
def test_advance_applies_source_count_from_brief(tmp_path, monkeypatch, config_lines, n_clips, expected):
    root = _root(tmp_path, config_lines)
    brief = valid_brief()
    brief["n_clips"] = n_clips
    plan = valid_plan(brief)
    (root / "brief.json").write_text(json.dumps(brief), encoding="utf-8")
    (root / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    seen = {}

    def capture(**kw):
        seen.update(kw["config"])
        raise _Stop

    monkeypatch.setattr(runner, "_advance_locked", capture)
    with pytest.raises(_Stop):
        runner.advance(root=root, brief=root / "brief.json", plan=root / "plan.json")
    for key, value in expected.items():
        assert seen.get(key) == value


def test_advance_without_brief_keeps_config(tmp_path, monkeypatch):
    root = _root(tmp_path, ["sleep_s: 0"])
    seen = {}

    def capture(**kw):
        seen.update(kw["config"])
        raise _Stop

    monkeypatch.setattr(runner, "_advance_locked", capture)
    with pytest.raises(_Stop):
        runner.advance(root=root)
    assert "max_analyze_videos" not in seen
