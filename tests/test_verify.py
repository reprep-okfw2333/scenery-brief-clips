import json
import shutil
import subprocess
from pathlib import Path

from scenery_brief_clips.analysis_cache import (
    ANALYSIS_CACHE_POLICY,
    ANALYSIS_MARKER_SCHEMA_VERSION,
    analysis_cache_path,
    analysis_marker_path,
    canonical_span_ms,
    export_cache_path,
)
from scenery_brief_clips.pipeline_analyze import analysis_generation_id, sha256_file
from scenery_brief_clips.verify import verify_run
from scenery_brief_clips.yt import export_marker_path


def _write(path: Path, payload) -> None:
    path.write_text(json.dumps(payload) + "\n")


def _refresh_manifest(run_dir: Path) -> None:
    manifest_path = run_dir / "analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    settings = manifest["settings"]
    ranked_sha256 = sha256_file(run_dir / "ranked.json")
    constraint_sha256 = sha256_file(run_dir / "constraint.json")
    excerpts_sha256 = sha256_file(run_dir / "excerpts.json")
    manifest.update(
        {
            "ranked_sha256": ranked_sha256,
            "constraint_sha256": constraint_sha256,
            "excerpts_sha256": excerpts_sha256,
            "generation_id": analysis_generation_id(
                ranked_sha256,
                constraint_sha256,
                excerpts_sha256,
                settings,
            ),
        }
    )
    _write(manifest_path, manifest)


def _valid_run(tmp_path: Path) -> tuple[Path, Path]:
    run_dir = tmp_path / "run"
    run_dir.mkdir(parents=True)
    constraint_path = run_dir / "constraint.json"
    ranked_path = run_dir / "ranked.json"
    media = analysis_cache_path(tmp_path / "analysis", "aaaaaaaaaaa", (10.0, 20.0))
    media.parent.mkdir()
    media.write_bytes(b"fake video bytes")
    _write(
        analysis_marker_path(media),
        {
            "schema_version": ANALYSIS_MARKER_SCHEMA_VERSION,
            "cache_policy": ANALYSIS_CACHE_POLICY,
            "video_id": "aaaaaaaaaaa",
            "span_ms": list(canonical_span_ms((10.0, 20.0))),
            "size_bytes": media.stat().st_size,
            "sha256": sha256_file(media),
        },
    )

    _write(
        constraint_path,
        {
            "min_width": 1920,
            "min_height": 1080,
            "aspect_min": 1.7,
            "aspect_max": 1.86,
            "n_clips": 20,
            "duration_min_s": 4.0,
            "duration_max_s": 12.0,
        },
    )
    _write(
        run_dir / "candidates.json",
        [{"video_id": "aaaaaaaaaaa", "width": 1920, "height": 1080, "aspect": 16 / 9}],
    )
    _write(
        ranked_path,
        [
            {
                "video_id": "aaaaaaaaaaa",
                "priority": "promising",
                "duration_s": 100.0,
                "windows": [{"start_s": 10, "end_s": 20}],
            }
        ],
    )
    copy = {
        "path": str(media),
        "span": [10.0, 20.0],
        "cache_key": media.name,
        "size_bytes": media.stat().st_size,
        "sha256": sha256_file(media),
    }
    _write(
        run_dir / "excerpts.json",
        [
            {
                "video_id": "aaaaaaaaaaa",
                "priority": "promising",
                "status": "complete",
                "plan_mode": "windows",
                "plan_ranges": [[10.0, 20.0]],
                "ranges": [
                    {
                        **copy,
                        "status": "complete",
                        "attempts": 1,
                        "attempt_errors": [],
                    }
                ],
                "copies": [copy],
                "errors": [],
                "ready_for_shortlist": True,
                "excerpts": [
                    {
                        "start_s": 12.0,
                        "end_s": 18.0,
                        "source_scene": [11.0, 19.0],
                        "analysis_span": [10.0, 20.0],
                        "analysis_cache_key": media.name,
                    }
                ],
            }
        ],
    )
    settings = {
        "max_videos": 1,
        "max_analysis_s": 120.0,
        "pad_s": 0.0,
        "min_scene_len_s": 0.5,
        "target_duration_s": 6.0,
        "duration_min_s": 4.0,
        "duration_max_s": 12.0,
        "acquisition_attempts": 2,
        "cache_policy": ANALYSIS_CACHE_POLICY,
    }
    ranked_sha256 = sha256_file(ranked_path)
    constraint_sha256 = sha256_file(constraint_path)
    excerpts_sha256 = sha256_file(run_dir / "excerpts.json")
    _write(
        run_dir / "analysis_manifest.json",
        {
            "schema_version": 3,
            "generation_id": analysis_generation_id(
                ranked_sha256,
                constraint_sha256,
                excerpts_sha256,
                settings,
            ),
            "ranked_sha256": ranked_sha256,
            "constraint_sha256": constraint_sha256,
            "excerpts_sha256": excerpts_sha256,
            "settings": settings,
        },
    )
    return run_dir, media


def _probe(_path: Path) -> dict:
    return {"video_streams": 1, "audio_streams": 0, "width": 1280, "height": 720, "duration_s": 10.0}


def test_verify_run_accepts_aligned_complete_run(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert report["ok"] is True
    assert report["errors"] == []
    assert report["n_excerpts"] == 1
    assert report["n_media"] == 1


def test_verify_can_review_moments_when_another_source_has_no_excerpt(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    other_id = "bbbbbbbbbbb"
    other_media = analysis_cache_path(tmp_path / "analysis", other_id, (10.0, 20.0))
    shutil.copyfile(media, other_media)
    marker = json.loads(analysis_marker_path(media).read_text())
    marker["video_id"] = other_id
    _write(analysis_marker_path(other_media), marker)

    candidates = json.loads((run_dir / "candidates.json").read_text())
    candidates.append({**candidates[0], "video_id": other_id})
    _write(run_dir / "candidates.json", candidates)
    ranked = json.loads((run_dir / "ranked.json").read_text())
    ranked.append({**ranked[0], "video_id": other_id})
    _write(run_dir / "ranked.json", ranked)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    empty = json.loads(json.dumps(rows[0]))
    empty["video_id"] = other_id
    empty["ready_for_shortlist"] = False
    empty["excerpts"] = []
    for copy in empty["copies"] + empty["ranges"]:
        copy["path"] = str(other_media)
        copy["cache_key"] = other_media.name
    rows.append(empty)
    _write(run_dir / "excerpts.json", rows)
    manifest_path = run_dir / "analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["settings"]["max_videos"] = 2
    _write(manifest_path, manifest)
    _refresh_manifest(run_dir)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is True
    assert report["n_excerpts"] == 1
    assert report["ready_for_shortlist"] is True
    assert report["all_analyzed_sources_have_excerpts"] is False
    assert report["n_sources_without_excerpts"] == 1
    assert any("completed with no usable excerpts" in w for w in report["warnings"])


def test_verify_run_flags_ranked_id_not_in_candidates(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    ranked_path = run_dir / "ranked.json"
    _write(ranked_path, [{"video_id": "bbbbbbbbbbb", "priority": "promising"}])
    _refresh_manifest(run_dir)
    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert report["ok"] is False
    assert any("not in candidates" in error for error in report["errors"])


def test_verify_detects_stale_ranked_input(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    ranked = json.loads((run_dir / "ranked.json").read_text())
    ranked[0]["windows"] = [{"start_s": 30, "end_s": 40}]
    _write(run_dir / "ranked.json", ranked)
    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert report["ok"] is False
    assert any("ranked.json changed after analysis" in error for error in report["errors"])


def test_verify_detects_excerpts_generation_mismatch(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows[0]["excerpts"] = []
    _write(run_dir / "excerpts.json", rows)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("excerpts.json does not match analysis manifest" in error for error in report["errors"])


def test_verify_rejects_partial_rows_and_missing_media(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows[0]["status"] = "partial"
    rows[0]["errors"] = [{"span": [10, 20], "error": "403"}]
    _write(run_dir / "excerpts.json", rows)
    media.unlink()
    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert report["ok"] is False
    assert any("status partial" in error for error in report["errors"])
    assert any("missing referenced media" in error for error in report["errors"])


def test_verify_rejects_nonfinite_and_out_of_bounds_excerpt(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows[0]["excerpts"][0]["start_s"] = float("nan")
    rows[0]["excerpts"][0]["end_s"] = 30.0
    _write(run_dir / "excerpts.json", rows)
    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert report["ok"] is False
    assert any("non-finite" in error for error in report["errors"])
    assert any("outside analysis span" in error for error in report["errors"])


def test_verify_rejects_missing_or_duplicate_analysis_rows(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    _write(run_dir / "excerpts.json", [])
    _refresh_manifest(run_dir)
    missing = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert missing["ok"] is False
    assert any("missing analysis row" in error for error in missing["errors"])

    run_dir, _ = _valid_run(tmp_path / "duplicate")
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows.append(dict(rows[0]))
    _write(run_dir / "excerpts.json", rows)
    _refresh_manifest(run_dir)
    duplicate = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert duplicate["ok"] is False
    assert any("duplicate analysis row" in error for error in duplicate["errors"])


def test_verify_reconciles_planned_ranges_outcomes_and_copies(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows[0]["ranges"] = []
    rows[0]["copies"] = []
    rows[0]["excerpts"] = []
    _write(run_dir / "excerpts.json", rows)
    _refresh_manifest(run_dir)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("range outcomes do not match plan" in error for error in report["errors"])
    assert any("copies do not match completed ranges" in error for error in report["errors"])


def test_verify_rejects_missing_path_and_span_cache_identity_mismatch(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows[0]["copies"][0].pop("path")
    _write(run_dir / "excerpts.json", rows)
    _refresh_manifest(run_dir)
    missing_path = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert missing_path["ok"] is False
    assert any("missing media path" in error for error in missing_path["errors"])

    run_dir, _ = _valid_run(tmp_path / "span")
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows[0]["ranges"][0]["span"] = [100.0, 110.0]
    rows[0]["copies"][0]["span"] = [100.0, 110.0]
    rows[0]["excerpts"][0]["analysis_span"] = [100.0, 110.0]
    _write(run_dir / "excerpts.json", rows)
    _refresh_manifest(run_dir)
    mismatched = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert mismatched["ok"] is False
    assert any("range outcomes do not match plan" in error for error in mismatched["errors"])
    assert any("cache key does not match" in error for error in mismatched["errors"])


def test_verify_rejects_media_outside_cache_and_bad_probe_geometry_duration(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    outside = tmp_path / "outside.mp4"
    outside.write_bytes(media.read_bytes())
    rows = json.loads((run_dir / "excerpts.json").read_text())
    rows[0]["copies"][0]["path"] = str(outside)
    rows[0]["ranges"][0]["path"] = str(outside)
    _write(run_dir / "excerpts.json", rows)
    _refresh_manifest(run_dir)
    escaped = verify_run(
        run_dir,
        analysis_dir=tmp_path / "analysis",
        probe_fn=_probe,
        decode_fn=lambda _path: None,
    )
    assert escaped["ok"] is False
    assert any("outside analysis cache" in error for error in escaped["errors"])

    run_dir, _ = _valid_run(tmp_path / "probe")
    bad_probe = lambda _path: {
        "video_streams": 1,
        "audio_streams": 0,
        "width": 0,
        "height": 720,
        "duration_s": 0.1,
    }
    bad_media = verify_run(run_dir, probe_fn=bad_probe, decode_fn=lambda _path: None)
    assert bad_media["ok"] is False
    assert any("invalid media dimensions" in error for error in bad_media["errors"])
    assert any("duration does not match span" in error for error in bad_media["errors"])


def test_verify_rejects_missing_settings_and_returns_structured_json_error(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    manifest["settings"].pop("cache_policy")
    _write(run_dir / "analysis_manifest.json", manifest)
    missing_setting = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert missing_setting["ok"] is False
    assert any("manifest setting cache_policy" in error for error in missing_setting["errors"])

    run_dir, _ = _valid_run(tmp_path / "json")
    (run_dir / "excerpts.json").write_text("{not-json")
    malformed = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)
    assert malformed["ok"] is False
    assert any("cannot load excerpts.json" in error for error in malformed["errors"])


def test_verify_returns_structured_error_for_invalid_constraint_types(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    constraint = json.loads((run_dir / "constraint.json").read_text())
    constraint["min_width"] = "not-an-int"
    _write(run_dir / "constraint.json", constraint)
    _refresh_manifest(run_dir)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("constraint fields are invalid" in error for error in report["errors"])


def _add_shortlist(tmp_path, run_dir, media, label=None):
    from scenery_brief_clips.shortlist import shortlist_apply_run

    rows = json.loads((run_dir / "excerpts.json").read_text())
    cache_key = rows[0]["copies"][0]["cache_key"]
    manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    (run_dir / "review.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "excerpts_sha256": sha256_file(run_dir / "excerpts.json"),
                "generation_id": manifest["generation_id"],
                "frames_per_moment": 2,
                "moments": [
                    {
                        "video_id": "aaaaaaaaaaa",
                        "excerpt_index": 0,
                        "start_s": 12.0,
                        "end_s": 18.0,
                        "analysis_cache_key": cache_key,
                        "frames": [],
                        "frame_hashes": ["0011223344556677"],
                    }
                ],
                "errors": [],
                "counts": {"moments": 1, "frames": 2, "errors": 0},
            }
        )
    )
    labels_path = run_dir / "shortlist_scores.json"
    labels_path.write_text(
        json.dumps(
            {
                "aaaaaaaaaaa": [
                    label
                    or {
                        "excerpt_index": 0,
                        "match": "keep",
                        "geo": "supported",
                        "scene_type": "mountains",
                    }
                ]
            }
        )
    )
    shortlist_apply_run(run_dir, labels_path)
    return labels_path


def _upgrade_to_dup_quota_run(run_dir: Path) -> None:
    """Rewrite the run so the dedup winner loses the quota race (3 moments, n_clips=1)."""
    from scenery_brief_clips.shortlist import shortlist_apply_run

    constraint_path = run_dir / "constraint.json"
    constraint = json.loads(constraint_path.read_text())
    constraint["n_clips"] = 1
    _write(constraint_path, constraint)
    constraint_sha256 = sha256_file(constraint_path)

    rows = json.loads((run_dir / "excerpts.json").read_text())
    media_name = rows[0]["copies"][0]["cache_key"]
    rows[0]["excerpts"] = [
        {
            "start_s": start,
            "end_s": end,
            "source_scene": [start, end],
            "analysis_span": [10.0, 20.0],
            "analysis_cache_key": media_name,
        }
        for start, end in ((10.0, 14.0), (14.5, 18.5), (15.5, 19.5))
    ]
    _write(run_dir / "excerpts.json", rows)
    excerpts_sha256 = sha256_file(run_dir / "excerpts.json")

    manifest_path = run_dir / "analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text())
    manifest["constraint_sha256"] = constraint_sha256
    manifest["excerpts_sha256"] = excerpts_sha256
    manifest["generation_id"] = analysis_generation_id(
        manifest["ranked_sha256"],
        constraint_sha256,
        excerpts_sha256,
        manifest["settings"],
    )
    _write(manifest_path, manifest)

    hashes = ["0123456789abcdef", "fedcba9876543210", "fedcba9876543210"]
    moments = [
        {
            "video_id": "aaaaaaaaaaa",
            "excerpt_index": index,
            "start_s": excerpt["start_s"],
            "end_s": excerpt["end_s"],
            "analysis_cache_key": excerpt["analysis_cache_key"],
            "frames": [],
            "frame_hashes": [hashes[index]],
        }
        for index, excerpt in enumerate(rows[0]["excerpts"])
    ]
    _write(
        run_dir / "review.json",
        {
            "schema_version": 1,
            "excerpts_sha256": excerpts_sha256,
            "generation_id": manifest["generation_id"],
            "frames_per_moment": 1,
            "moments": moments,
            "counts": {"moments": len(moments)},
        },
    )
    labels_path = run_dir / "shortlist_scores.json"
    _write(
        labels_path,
        {
            "aaaaaaaaaaa": [
                {"excerpt_index": index, "match": "keep", "geo": "supported", "scene_type": "x"}
                for index in range(3)
            ]
        },
    )
    shortlist_apply_run(run_dir, labels_path)


def test_verify_accepts_duplicate_winner_beyond_quota(tmp_path):
    run_dir, _media = _valid_run(tmp_path)
    _upgrade_to_dup_quota_run(run_dir)

    doc = json.loads((run_dir / "shortlist.json").read_text())
    assert [(entry["video_id"], entry["excerpt_index"]) for entry in doc["selected"]] == [
        ("aaaaaaaaaaa", 0)
    ]
    reasons = {entry["excerpt_index"]: entry["reasons"] for entry in doc["excluded"]}
    assert reasons[1] == ["beyond n_clips"]
    assert reasons[2] == ["duplicate of aaaaaaaaaaa:1"]

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is True, report["errors"]
    assert report["shortlist"]["n_selected"] == 1


def test_duplicate_reference_semantics():
    from scenery_brief_clips.verify import _duplicate_reference_error

    candidates = {("aaa", 0), ("aaa", 1), ("bbb", 0), ("bbb", 1)}
    excluded_by_key = {("aaa", 1): ["beyond n_clips"], ("bbb", 0): ["duplicate of aaa:1"]}

    assert _duplicate_reference_error(("bbb", 0), "aaa:1", candidates, excluded_by_key) is None
    assert _duplicate_reference_error(("bbb", 0), "aaa:0", candidates, excluded_by_key) is None
    assert "does not match" in _duplicate_reference_error(
        ("bbb", 0), "zzz:0", candidates, excluded_by_key
    )
    assert "itself" in _duplicate_reference_error(("bbb", 0), "bbb:0", candidates, excluded_by_key)
    chained = {**excluded_by_key, ("ccc", 0): ["duplicate of bbb:0"]}
    assert "another duplicate" in _duplicate_reference_error(("ccc", 0), "bbb:0", candidates, chained)


def test_verify_flags_review_not_bound_to_generation(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)

    review = json.loads((run_dir / "review.json").read_text())
    review["excerpts_sha256"] = "0" * 64
    _write(run_dir / "review.json", review)
    doc = json.loads((run_dir / "shortlist.json").read_text())
    doc["bindings"]["review_sha256"] = sha256_file(run_dir / "review.json")
    _write(run_dir / "shortlist.json", doc)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("does not match the analysis generation" in error for error in report["errors"])


def test_verify_flags_labels_file_outside_run_dir(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)

    outside = tmp_path / "outside-labels.json"
    _write(
        outside,
        {
            "aaaaaaaaaaa": [
                {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "x"}
            ]
        },
    )
    doc = json.loads((run_dir / "shortlist.json").read_text())
    doc["bindings"]["labels_path"] = str(outside)
    doc["bindings"]["labels_sha256"] = sha256_file(outside)
    _write(run_dir / "shortlist.json", doc)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("must stay inside the run dir" in error for error in report["errors"])


def test_verify_flags_labels_bound_to_other_generation(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    labels_path = _add_shortlist(tmp_path, run_dir, media)

    labels = json.loads(labels_path.read_text())
    labels["excerpts_sha256"] = "0" * 64
    _write(labels_path, labels)
    doc = json.loads((run_dir / "shortlist.json").read_text())
    doc["bindings"]["labels_sha256"] = sha256_file(labels_path)
    _write(run_dir / "shortlist.json", doc)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("analysis generation" in error for error in report["errors"])


def _probe_export(path: Path) -> dict:
    if any(token in Path(path).name for token in ("_e0_10000-14000.mp4", "_e1_15500-19500.mp4")):
        n_frames = 120
        last_end_s = 4.0
    else:
        n_frames = 180
        last_end_s = 6.0
    return {
        "width": 1280,
        "height": 720,
        "fps_num": 30,
        "fps_den": 1,
        "n_frames": n_frames,
        "last_end_s": last_end_s,
        "max_gap_s": 0.0333,
        "first_pts_s": 0.0,
    }


def _add_export(
    tmp_path,
    run_dir,
    *,
    mutate=None,
    clip_bytes=b"fake clip bytes",
    clip_source=None,
):
    from scenery_brief_clips.export import (
        EXPORT_POLICY,
        EXPORT_RECIPE,
        EXPORT_SCHEMA_VERSION,
        POINTER_SCHEMA_VERSION,
    )

    root = Path(tmp_path)
    theme = "test-theme"
    theme_dir = root / "out" / theme
    clips_dir = theme_dir / "clips"
    clips_dir.mkdir(parents=True)
    shortlist = json.loads((run_dir / "shortlist.json").read_text())
    analysis_manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    item = shortlist["selected"][0]
    start_ms = round(float(item["start_s"]) * 1000)
    end_ms = round(float(item["end_s"]) * 1000)
    acq_start_ms = start_ms - 2000
    acq_end_ms = end_ms + 2000
    name = f'{item["video_id"]}_e{item["excerpt_index"]}_{start_ms}-{end_ms}.mp4'
    clip = clips_dir / name
    if clip_source is None:
        clip.write_bytes(clip_bytes)
    else:
        shutil.copyfile(clip_source, clip)

    plan = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "theme": theme,
        "policy": EXPORT_POLICY,
        "recipe": EXPORT_RECIPE,
        "margin_s": 2.0,
        "max_height": 720,
        "shortlist_sha256": sha256_file(run_dir / "shortlist.json"),
        "generation_id": analysis_manifest["generation_id"],
        "n_clips": 20,
        "moments": [
            {
                "video_id": item["video_id"],
                "excerpt_index": item["excerpt_index"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "acq_start_ms": acq_start_ms,
                "acq_end_ms": acq_end_ms,
                "status": "ready",
                "reason": None,
                "spec": {
                    "format_id": "136",
                    "width": 1280,
                    "height": 720,
                    "codec": "avc1.4d401f",
                },
            }
        ],
    }
    _write(theme_dir / "plan.json", plan)
    doc = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "theme": theme,
        "policy": EXPORT_POLICY,
        "recipe": EXPORT_RECIPE,
        "margin_s": 2.0,
        "max_height": 720,
        "toolchain": {"ffmpeg": "test"},
        "shortlist_sha256": sha256_file(run_dir / "shortlist.json"),
        "generation_id": analysis_manifest["generation_id"],
        "plan_sha256": sha256_file(theme_dir / "plan.json"),
        "constraint": {"min_width": 1920, "min_height": 1080, "aspect_min": 1.7, "aspect_max": 1.86},
        "clips": [
            {
                "video_id": item["video_id"],
                "excerpt_index": item["excerpt_index"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "acq_span_ms": [acq_start_ms, acq_end_ms],
                "mapping_k_ms": acq_start_ms,
                "format_id": "136",
                "codec": "avc1.4d401f",
                "width": 1280,
                "height": 720,
                "file": f"clips/{name}",
                "size_bytes": clip.stat().st_size,
                "sha256": sha256_file(clip),
                "duration_ms": end_ms - start_ms,
                "n_frames": 180,
                "acq_sha256": "a" * 64,
                "recipe": EXPORT_RECIPE,
            }
        ],
        "failed": [],
        "counts": {
            "requested": 20,
            "selected": 1,
            "exported": 1,
            "failed": 0,
            "export_complete": True,
            "request_fulfilled": False,
        },
    }
    if mutate is not None:
        mutate(doc, theme_dir, clip)
    _write(theme_dir / "manifest.json", doc)
    _write(
        run_dir / "export.json",
        {
            "schema_version": POINTER_SCHEMA_VERSION,
            "run_id": run_dir.name,
            "theme": theme,
            "manifest_path": f"out/{theme}/manifest.json",
            "manifest_sha256": sha256_file(theme_dir / "manifest.json"),
            "policy": EXPORT_POLICY,
        },
    )
    return theme_dir, clip


def _two_moment_run(tmp_path):
    from scenery_brief_clips.shortlist import shortlist_apply_run

    run_dir, _media = _valid_run(tmp_path)
    rows = json.loads((run_dir / "excerpts.json").read_text())
    cache_key = rows[0]["copies"][0]["cache_key"]
    rows[0]["excerpts"] = [
        {
            "start_s": 10.0,
            "end_s": 14.0,
            "source_scene": [10.0, 14.5],
            "analysis_span": [10.0, 20.0],
            "analysis_cache_key": cache_key,
        },
        {
            "start_s": 15.5,
            "end_s": 19.5,
            "source_scene": [15.0, 19.9],
            "analysis_span": [10.0, 20.0],
            "analysis_cache_key": cache_key,
        },
    ]
    _write(run_dir / "excerpts.json", rows)
    _refresh_manifest(run_dir)
    manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    _write(
        run_dir / "review.json",
        {
            "schema_version": 1,
            "excerpts_sha256": sha256_file(run_dir / "excerpts.json"),
            "generation_id": manifest["generation_id"],
            "frames_per_moment": 2,
            "moments": [
                {
                    "video_id": "aaaaaaaaaaa",
                    "excerpt_index": 0,
                    "start_s": 10.0,
                    "end_s": 14.0,
                    "analysis_cache_key": cache_key,
                    "frames": [],
                    "frame_hashes": ["0011223344556677"],
                },
                {
                    "video_id": "aaaaaaaaaaa",
                    "excerpt_index": 1,
                    "start_s": 15.5,
                    "end_s": 19.5,
                    "analysis_cache_key": cache_key,
                    "frames": [],
                    "frame_hashes": ["fedcba9876543210"],
                },
            ],
            "errors": [],
            "counts": {"moments": 2, "frames": 4, "errors": 0},
        },
    )
    labels_path = run_dir / "shortlist_scores.json"
    _write(
        labels_path,
        {
            "aaaaaaaaaaa": [
                {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "mountains"},
                {"excerpt_index": 1, "match": "keep", "geo": "supported", "scene_type": "forest"},
            ]
        },
    )
    shortlist = shortlist_apply_run(run_dir, labels_path)
    assert len(shortlist["selected"]) == 2
    return run_dir


def _add_two_moment_export(tmp_path, run_dir, *, mutate=None, duration_delta=0):
    from scenery_brief_clips.export import (
        EXPORT_POLICY,
        EXPORT_RECIPE,
        EXPORT_SCHEMA_VERSION,
        POINTER_SCHEMA_VERSION,
    )

    root = Path(tmp_path)
    theme = "test-theme"
    theme_dir = root / "out" / theme
    clips_dir = theme_dir / "clips"
    clips_dir.mkdir(parents=True)
    shortlist = json.loads((run_dir / "shortlist.json").read_text())
    analysis_manifest = json.loads((run_dir / "analysis_manifest.json").read_text())
    selected = sorted(shortlist["selected"], key=lambda item: int(item["excerpt_index"]))
    assert [item["excerpt_index"] for item in selected] == [0, 1]
    acquisition_spans = [(8000, 16000), (13500, 21500)]
    moments = []
    clips = []
    for item, (acq_start_ms, acq_end_ms) in zip(selected, acquisition_spans):
        start_ms = round(float(item["start_s"]) * 1000)
        end_ms = round(float(item["end_s"]) * 1000)
        name = f'{item["video_id"]}_e{item["excerpt_index"]}_{start_ms}-{end_ms}.mp4'
        clip = clips_dir / name
        clip.write_bytes(f'fake clip bytes {item["excerpt_index"]}'.encode())
        clips.append(clip)
        moments.append(
            {
                "video_id": item["video_id"],
                "excerpt_index": item["excerpt_index"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "acq_start_ms": acq_start_ms,
                "acq_end_ms": acq_end_ms,
                "status": "ready",
                "reason": None,
                "spec": {
                    "format_id": "136",
                    "width": 1280,
                    "height": 720,
                    "codec": "avc1.4d401f",
                },
            }
        )

    plan = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "theme": theme,
        "policy": EXPORT_POLICY,
        "recipe": EXPORT_RECIPE,
        "margin_s": 2.0,
        "max_height": 720,
        "shortlist_sha256": sha256_file(run_dir / "shortlist.json"),
        "generation_id": analysis_manifest["generation_id"],
        "n_clips": 20,
        "moments": moments,
    }
    _write(theme_dir / "plan.json", plan)
    clip_entries = []
    for item, (acq_start_ms, acq_end_ms), clip in zip(selected, acquisition_spans, clips):
        start_ms = round(float(item["start_s"]) * 1000)
        end_ms = round(float(item["end_s"]) * 1000)
        clip_entries.append(
            {
                "video_id": item["video_id"],
                "excerpt_index": item["excerpt_index"],
                "start_ms": start_ms,
                "end_ms": end_ms,
                "acq_span_ms": [acq_start_ms, acq_end_ms],
                "mapping_k_ms": acq_start_ms,
                "format_id": "136",
                "codec": "avc1.4d401f",
                "width": 1280,
                "height": 720,
                "file": f"clips/{clip.name}",
                "size_bytes": clip.stat().st_size,
                "sha256": sha256_file(clip),
                "duration_ms": end_ms - start_ms + duration_delta,
                "n_frames": 120,
                "acq_sha256": "a" * 64,
                "recipe": EXPORT_RECIPE,
            }
        )
    doc = {
        "schema_version": EXPORT_SCHEMA_VERSION,
        "run_id": run_dir.name,
        "theme": theme,
        "policy": EXPORT_POLICY,
        "recipe": EXPORT_RECIPE,
        "margin_s": 2.0,
        "max_height": 720,
        "toolchain": {"ffmpeg": "test"},
        "shortlist_sha256": sha256_file(run_dir / "shortlist.json"),
        "generation_id": analysis_manifest["generation_id"],
        "plan_sha256": sha256_file(theme_dir / "plan.json"),
        "constraint": {"min_width": 1920, "min_height": 1080, "aspect_min": 1.7, "aspect_max": 1.86},
        "clips": clip_entries,
        "failed": [],
        "counts": {
            "requested": 20,
            "selected": 2,
            "exported": 2,
            "failed": 0,
            "export_complete": True,
            "request_fulfilled": False,
        },
    }
    if mutate is not None:
        mutate(doc, theme_dir, clips)
    _write(theme_dir / "manifest.json", doc)
    _write(
        run_dir / "export.json",
        {
            "schema_version": POINTER_SCHEMA_VERSION,
            "run_id": run_dir.name,
            "theme": theme,
            "manifest_path": f"out/{theme}/manifest.json",
            "manifest_sha256": sha256_file(theme_dir / "manifest.json"),
            "policy": EXPORT_POLICY,
        },
    )
    return theme_dir, clips


def test_verify_checks_each_clip_against_its_own_plan_moment(tmp_path):
    run_dir = _two_moment_run(tmp_path)
    _add_two_moment_export(tmp_path, run_dir)
    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )
    assert report["ok"] is True, report["errors"]
    assert report["export"]["acquisitions_checked"] == 0

    def wrong_second_format(doc, theme_dir, _clips):
        plan_path = theme_dir / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["moments"][1]["spec"]["format_id"] = "999"
        _write(plan_path, plan)
        doc["plan_sha256"] = sha256_file(plan_path)

    format_root = tmp_path / "format-mutation"
    format_run = _two_moment_run(format_root)
    _theme_dir, format_clips = _add_two_moment_export(format_root, format_run, mutate=wrong_second_format)
    format_report = verify_run(
        format_run,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=format_root,
        export_probe_fn=_probe_export,
    )
    format_errors = [
        error
        for error in format_report["errors"]
        if "export clip format id does not match the plan" in error
    ]
    assert format_errors == [
        f"export clip format id does not match the plan: clips/{format_clips[1].name}"
    ]
    assert format_clips[0].name not in " ".join(format_errors)

    duration_root = tmp_path / "duration-tolerance"
    duration_run = _two_moment_run(duration_root)
    _add_two_moment_export(duration_root, duration_run, duration_delta=11)
    within_tolerance = verify_run(
        duration_run,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=duration_root,
        export_probe_fn=_probe_export,
    )
    assert within_tolerance["ok"] is True, within_tolerance["errors"]
    assert not any("manifest duration does not match" in error for error in within_tolerance["errors"])

    def too_long(doc, _theme_dir, _clips):
        doc["clips"][0]["duration_ms"] = 4500

    long_root = tmp_path / "duration-error"
    long_run = _two_moment_run(long_root)
    _theme_dir, long_clips = _add_two_moment_export(long_root, long_run, mutate=too_long)
    too_long_report = verify_run(
        long_run,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=long_root,
        export_probe_fn=_probe_export,
    )
    assert any(
        error == f"export clip manifest duration does not match the interval: clips/{long_clips[0].name}"
        for error in too_long_report["errors"]
    )


def test_verify_proves_a_good_export(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)
    _add_export(tmp_path, run_dir)

    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )

    assert report["ok"] is True, report["errors"]
    assert report["export"]["present"] is True
    assert report["export"]["exported"] == 1
    assert report["export"]["export_complete"] is True
    assert report["export"]["acquisitions_checked"] == 0


def test_verify_require_export_flag(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)

    plain = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )
    assert plain["ok"] is True
    assert plain["export"] is None

    required = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        require_export=True,
        export_probe_fn=_probe_export,
    )
    assert required["ok"] is False
    assert any("export verification requested" in error for error in required["errors"])


def test_verify_flags_tampered_export_clip_bytes(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)
    _, clip = _add_export(tmp_path, run_dir)
    clip.write_bytes(b"tampered bytes")

    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )

    assert report["ok"] is False
    assert any("export clip hash changed" in error for error in report["errors"])
    assert any("export clip size changed" in error for error in report["errors"])


def test_verify_flags_missing_and_unlisted_export_clips(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)
    _, clip = _add_export(tmp_path, run_dir)
    clip.unlink()

    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )
    assert report["ok"] is False
    assert any("missing or a symlink" in error for error in report["errors"])

    root2 = tmp_path / "extra"
    run_dir2, media2 = _valid_run(root2)
    _add_shortlist(root2, run_dir2, media2)
    theme_dir2, _clip2 = _add_export(root2, run_dir2)
    (theme_dir2 / "clips" / "extra_file.mp4").write_bytes(b"x")

    report2 = verify_run(
        run_dir2,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=root2,
        export_probe_fn=_probe_export,
    )
    assert report2["ok"] is False
    assert any("unlisted file in the export" in error for error in report2["errors"])


def test_verify_flags_stale_or_mismatched_export_metadata(tmp_path):
    def stale(doc, _theme_dir, _clip):
        doc["shortlist_sha256"] = "0" * 64

    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)
    _add_export(tmp_path, run_dir, mutate=stale)
    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )
    assert any("stale for the current shortlist" in error for error in report["errors"])

    def foreign(doc, _theme_dir, _clip):
        doc["clips"][0]["excerpt_index"] = 9

    root2 = tmp_path / "foreign"
    run_dir2, media2 = _valid_run(root2)
    _add_shortlist(root2, run_dir2, media2)
    _add_export(root2, run_dir2, mutate=foreign)
    report2 = verify_run(
        run_dir2,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=root2,
        export_probe_fn=_probe_export,
    )
    assert any("not a selected moment" in error for error in report2["errors"])

    def bad_counts(doc, _theme_dir, _clip):
        doc["counts"]["exported"] = 2

    root3 = tmp_path / "counts"
    run_dir3, media3 = _valid_run(root3)
    _add_shortlist(root3, run_dir3, media3)
    _add_export(root3, run_dir3, mutate=bad_counts)
    report3 = verify_run(
        run_dir3,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=root3,
        export_probe_fn=_probe_export,
    )
    assert any("counts.exported" in error for error in report3["errors"])


def test_verify_flags_export_clip_duration_and_aspect(tmp_path):
    def duration_probe(_path):
        return {**_probe_export(_path), "last_end_s": 8.0}

    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)
    _add_export(tmp_path, run_dir)
    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=duration_probe,
    )
    assert any("planned 6000ms" in error for error in report["errors"])

    def wider(doc, theme_dir, _clip):
        doc["clips"][0]["width"] = 1000
        plan_path = theme_dir / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["moments"][0]["spec"]["width"] = 1000
        _write(plan_path, plan)
        doc["plan_sha256"] = sha256_file(plan_path)

    def aspect_probe(_path):
        return {**_probe_export(_path), "width": 1000}

    root2 = tmp_path / "aspect"
    run_dir2, media2 = _valid_run(root2)
    _add_shortlist(root2, run_dir2, media2)
    _add_export(root2, run_dir2, mutate=wider)
    report2 = verify_run(
        run_dir2,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=root2,
        export_probe_fn=aspect_probe,
    )
    assert any("not 16:9" in error for error in report2["errors"])


def test_verify_rejects_export_above_cap_or_below_floor(tmp_path):
    def set_dimensions(width, height):
        def mutate(doc, theme_dir, _clip):
            doc["clips"][0].update({"width": width, "height": height})
            plan_path = theme_dir / "plan.json"
            plan = json.loads(plan_path.read_text())
            plan["moments"][0]["spec"].update({"width": width, "height": height})
            _write(plan_path, plan)
            doc["plan_sha256"] = sha256_file(plan_path)

        return mutate

    def probe_dimensions(width, height):
        def probe(_path):
            return {**_probe_export(_path), "width": width, "height": height}

        return probe

    cases = (("above", 1920, 1080), ("below", 640, 480))
    for label, width, height in cases:
        root = tmp_path / label
        run_dir, media = _valid_run(root)
        _add_shortlist(root, run_dir, media)
        _add_export(root, run_dir, mutate=set_dimensions(width, height))
        report = verify_run(
            run_dir,
            probe_fn=_probe,
            decode_fn=lambda _path: None,
            root=root,
            export_probe_fn=probe_dimensions(width, height),
        )
        assert report["ok"] is False
        assert any("outside the 720p..720p band" in error for error in report["errors"])


def test_verify_requires_manifest_max_height(tmp_path):
    def remove_cap(doc, _theme_dir, _clip):
        del doc["max_height"]

    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)
    _add_export(tmp_path, run_dir, mutate=remove_cap)
    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )
    assert any("no valid max_height" in error for error in report["errors"])


def test_verify_flags_plan_mismatches(tmp_path):
    def rewrite_plan(root, mutation):
        run_dir, media = _valid_run(root)
        _add_shortlist(root, run_dir, media)
        _add_export(root, run_dir, mutate=mutation)
        return verify_run(
            run_dir,
            probe_fn=_probe,
            decode_fn=lambda _path: None,
            root=root,
            export_probe_fn=_probe_export,
        )

    def dimensions_mismatch(doc, theme_dir, _clip):
        plan_path = theme_dir / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["moments"][0]["spec"]["height"] = 704
        _write(plan_path, plan)
        doc["plan_sha256"] = sha256_file(plan_path)

    dimensions = rewrite_plan(tmp_path / "dimensions", dimensions_mismatch)
    assert any("dimensions do not match the plan" in error for error in dimensions["errors"])

    def missing_moment(doc, theme_dir, _clip):
        plan_path = theme_dir / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["moments"] = []
        _write(plan_path, plan)
        doc["plan_sha256"] = sha256_file(plan_path)

    missing = rewrite_plan(tmp_path / "missing", missing_moment)
    assert any("missing selected moment" in error for error in missing["errors"])

    def duplicate_moment(doc, theme_dir, _clip):
        plan_path = theme_dir / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["moments"].append(dict(plan["moments"][0]))
        _write(plan_path, plan)
        doc["plan_sha256"] = sha256_file(plan_path)

    duplicate = rewrite_plan(tmp_path / "duplicate", duplicate_moment)
    assert any("lists moment aaaaaaaaaaa:0 more than once" in error for error in duplicate["errors"])

    def unplannable(doc, theme_dir, _clip):
        plan_path = theme_dir / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["moments"][0]["status"] = "unplannable"
        _write(plan_path, plan)
        doc["plan_sha256"] = sha256_file(plan_path)

    unavailable = rewrite_plan(tmp_path / "unplannable", unplannable)
    assert any("which the plan marked unplannable" in error for error in unavailable["errors"])


def test_verify_flags_staging_and_foreign_files(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)
    theme_dir, _clip = _add_export(tmp_path, run_dir)
    (theme_dir / "clips" / "extra.staging-deadbeef.mp4").write_bytes(b"partial")
    (theme_dir / "clips" / "notes.txt").write_text("not a clip")

    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )
    assert any("publication is in progress or was interrupted" in error for error in report["errors"])
    assert any("unlisted file in the export" in error for error in report["errors"])


def test_verify_checks_acquisition_provenance(tmp_path):
    cache_dir = tmp_path / "data" / "cache" / "export"
    policy = "v2-export-cap-copyts.136"
    acquisition = export_cache_path(cache_dir, "aaaaaaaaaaa", (10.0, 20.0), policy=policy)
    acquisition.parent.mkdir(parents=True)
    original_bytes = b"acquisition bytes"
    acquisition.write_bytes(original_bytes)
    _write(
        export_marker_path(acquisition),
        {"first_pts_ms": 10000, "width": 1280, "height": 720},
    )

    def bind_acquisition(doc, _theme_dir, _clip):
        doc["clips"][0]["acq_sha256"] = sha256_file(acquisition)

    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)
    _add_export(tmp_path, run_dir, mutate=bind_acquisition)
    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )
    assert report["ok"] is True, report["errors"]
    assert report["export"]["acquisitions_checked"] == 1

    acquisition.write_bytes(b"rewritten acquisition bytes")
    changed = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )
    assert any("acquisition cache content changed" in error for error in changed["errors"])

    acquisition.write_bytes(original_bytes)
    marker = json.loads(export_marker_path(acquisition).read_text())
    marker["first_pts_ms"] = 9000
    _write(export_marker_path(acquisition), marker)
    remapped = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=_probe_export,
    )
    assert any("acquisition mapping key changed" in error for error in remapped["errors"])


def test_verify_real_media_export_probe(tmp_path):
    clip = tmp_path / "real-720.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=1280x720:rate=30:duration=6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            str(clip),
        ],
        check=True,
    )
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)
    _add_export(tmp_path, run_dir, clip_source=clip)
    report = verify_run(
        run_dir,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=tmp_path,
        export_probe_fn=None,
    )
    assert report["ok"] is True, report["errors"]

    root2 = tmp_path / "real-low"
    root2.mkdir(parents=True)
    clip2 = root2 / "real-480.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-y",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "testsrc2=size=640x480:rate=30:duration=6",
            "-c:v",
            "libx264",
            "-preset",
            "ultrafast",
            "-crf",
            "30",
            "-pix_fmt",
            "yuv420p",
            "-g",
            "30",
            str(clip2),
        ],
        check=True,
    )

    def low_dimensions(doc, theme_dir, _clip):
        doc["clips"][0].update({"width": 640, "height": 480})
        plan_path = theme_dir / "plan.json"
        plan = json.loads(plan_path.read_text())
        plan["moments"][0]["spec"].update({"width": 640, "height": 480})
        _write(plan_path, plan)
        doc["plan_sha256"] = sha256_file(plan_path)

    run_dir2, media2 = _valid_run(root2)
    _add_shortlist(root2, run_dir2, media2)
    _add_export(root2, run_dir2, mutate=low_dimensions, clip_source=clip2)
    report2 = verify_run(
        run_dir2,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
        root=root2,
        export_probe_fn=None,
    )
    assert report2["ok"] is False
    assert any("outside the 720p..720p band" in error for error in report2["errors"])


def test_verify_checks_valid_shortlist_present(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is True, report["errors"]
    assert report["shortlist"]["present"] is True
    assert report["shortlist"]["n_selected"] == 1
    assert report["shortlist"]["shortfall"] == 19


def test_verify_flags_stale_shortlist_after_new_analysis_generation(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)

    rows = json.loads((run_dir / "excerpts.json").read_text())
    (run_dir / "excerpts.json").write_text(json.dumps(rows, indent=2) + "\n")
    _refresh_manifest(run_dir)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("shortlist is stale" in error for error in report["errors"])


def test_verify_flags_tampered_shortlist_selection(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)

    doc = json.loads((run_dir / "shortlist.json").read_text())
    doc["selected"] = []
    _write(run_dir / "shortlist.json", doc)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("does not match its inputs" in error for error in report["errors"])


def test_verify_flags_missing_shortlist_labels_file(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    labels_path = _add_shortlist(tmp_path, run_dir, media)
    labels_path.unlink()

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("labels file is missing" in error for error in report["errors"])


def test_verify_flags_selected_moment_with_unknown_media(tmp_path):
    run_dir, media = _valid_run(tmp_path)
    _add_shortlist(tmp_path, run_dir, media)

    doc = json.loads((run_dir / "shortlist.json").read_text())
    doc["selected"][0]["analysis_cache_key"] = "ghost.mp4"
    _write(run_dir / "shortlist.json", doc)

    report = verify_run(run_dir, probe_fn=_probe, decode_fn=lambda _path: None)

    assert report["ok"] is False
    assert any("unknown analysis media" in error for error in report["errors"])


def test_verify_ignores_unreferenced_shared_cache_files(tmp_path):
    run_dir, _ = _valid_run(tmp_path)
    unrelated = tmp_path / "analysis" / "unrelated-empty.mp4"
    unrelated.write_bytes(b"")
    report = verify_run(
        run_dir,
        analysis_dir=unrelated.parent,
        probe_fn=_probe,
        decode_fn=lambda _path: None,
    )
    assert report["ok"] is True
