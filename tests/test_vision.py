from scenery_brief_clips.vision import apply_scores_run, apply_tile_scores


def test_keep_tiles_make_promising_windows():
    tiles = [
        {"t_s": 10.0, "ok": True, "dark": False},
        {"t_s": 20.0, "ok": True, "dark": False},
        {"t_s": 400.0, "ok": True, "dark": False},
    ]
    scores = [
        {"t_s": 10.0, "label": "keep"},
        {"t_s": 20.0, "label": "keep"},
        {"t_s": 400.0, "label": "reject"},
    ]
    result = apply_tile_scores(tiles, scores, interval_s=10.0)
    assert result.priority == "promising"
    assert len(result.windows) == 1
    assert result.windows[0]["start_s"] == 10.0
    assert result.windows[0]["end_s"] == 30.0


def test_all_reject_is_low_with_no_windows():
    tiles = [{"t_s": 1.0, "ok": True, "dark": False}, {"t_s": 2.0, "ok": True, "dark": False}]
    scores = [{"t_s": 1.0, "label": "reject"}, {"t_s": 2.0, "label": "reject"}]
    result = apply_tile_scores(tiles, scores, interval_s=1.0)
    assert result.priority == "low"
    assert result.windows == []
    assert result.reason == "vision_reject"


def test_only_uncertain_stays_uncertain_but_keeps_windows():
    tiles = [{"t_s": 5.0, "ok": True, "dark": False}]
    scores = [{"t_s": 5.0, "label": "uncertain"}]
    result = apply_tile_scores(tiles, scores, interval_s=10.0)
    assert result.priority == "uncertain"
    assert result.windows
    assert result.reason == "vision_uncertain"


def test_unscored_non_dark_tiles_are_uncertain():
    tiles = [{"t_s": 5.0, "ok": True, "dark": False}]
    result = apply_tile_scores(tiles, scores=[], interval_s=10.0)
    assert result.priority == "uncertain"
    assert result.reason == "no_vision_backend"


def test_sparse_score_does_not_label_neighboring_tiles():
    tiles = [
        {"t_s": 0.0, "ok": True, "dark": False, "path": "/tiles/0.jpg"},
        {"t_s": 10.0, "ok": True, "dark": False, "path": "/tiles/10.jpg"},
        {"t_s": 20.0, "ok": True, "dark": False, "path": "/tiles/20.jpg"},
    ]
    result = apply_tile_scores(
        tiles,
        scores=[{"path": "/tiles/0.jpg", "t_s": 0.0, "label": "keep"}],
        interval_s=10.0,
    )
    assert [t.get("vision_label") for t in result.tiles] == ["keep", "uncertain", "uncertain"]
    assert result.windows == [{"start_s": 0.0, "end_s": 10.0}]


def test_reject_between_keep_tiles_splits_windows():
    tiles = [
        {"t_s": 0.0, "ok": True, "dark": False},
        {"t_s": 10.0, "ok": True, "dark": False},
        {"t_s": 20.0, "ok": True, "dark": False},
    ]
    scores = [
        {"t_s": 0.0, "label": "keep"},
        {"t_s": 10.0, "label": "reject"},
        {"t_s": 20.0, "label": "keep"},
    ]
    result = apply_tile_scores(tiles, scores, interval_s=10.0)
    assert result.windows == [
        {"start_s": 0.0, "end_s": 10.0},
        {"start_s": 20.0, "end_s": 30.0},
    ]


def test_no_tiles_is_unknown_not_low():
    result = apply_tile_scores([], scores=[], interval_s=10.0)
    assert result.priority == "unknown"
    assert result.reason == "no_storyboard"


def test_apply_scores_preserves_missing_metadata_unknown(tmp_path):
    import json

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    original = [
        {
            "video_id": "missingxxxx",
            "priority": "unknown",
            "reason": "missing_metadata",
            "windows": [],
        }
    ]
    (run_dir / "ranked.json").write_text(json.dumps(original))
    scores = tmp_path / "scores.json"
    scores.write_text("{}")
    rows = apply_scores_run(run_dir, scores)
    assert rows[0]["priority"] == "unknown"
    assert rows[0]["reason"] == "missing_metadata"


def test_apply_scores_rejects_unknown_label_without_mutating_the_run(tmp_path):
    import json

    import pytest

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ranked_path = run_dir / "ranked.json"
    original = [
        {
            "video_id": "abcdefghijk",
            "priority": "uncertain",
            "interval_s": 10.0,
            "tiles": [{"t_s": 0.0, "ok": True, "dark": False}],
            "windows": [{"start_s": 0.0, "end_s": 10.0}],
        }
    ]
    ranked_path.write_text(json.dumps(original))
    scores = tmp_path / "scores.json"
    scores.write_text(json.dumps({"abcdefghijk": [{"t_s": 0.0, "label": "keeper"}]}))

    with pytest.raises(ValueError, match="unknown vision label"):
        apply_scores_run(run_dir, scores)

    assert json.loads(ranked_path.read_text()) == original
    assert not (run_dir / "ranked_before_vision.json").exists()


def test_apply_scores_rejects_malformed_scores_shape(tmp_path):
    import json

    import pytest

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    ranked_path = run_dir / "ranked.json"
    ranked_path.write_text(json.dumps([{"video_id": "abcdefghijk", "priority": "uncertain"}]))

    bad_top_level = tmp_path / "bad-top.json"
    bad_top_level.write_text("[]")
    with pytest.raises(ValueError, match="must be a JSON object"):
        apply_scores_run(run_dir, bad_top_level)

    bad_entries = tmp_path / "bad-entries.json"
    bad_entries.write_text(json.dumps({"abcdefghijk": {"t_s": 0.0, "label": "keep"}}))
    with pytest.raises(ValueError, match="must be a list"):
        apply_scores_run(run_dir, bad_entries)
