import json
from pathlib import Path

import pytest

from scenery_brief_clips.analysis_cache import sha256_file
from scenery_brief_clips.cli import doctor, main


def test_doctor_reports_required_binaries(tmp_path):
    report = doctor(tmp_path)
    assert "yt_dlp" in report
    assert "ffmpeg" in report
    assert "ffprobe" in report
    assert "node" in report
    assert "export_policy" in report
    assert Path(report["tmp_dir"]).is_dir()
    assert report["tmp_dir"].startswith(str(tmp_path))


def test_explain_prompt_prints_constraint_json(capsys):
    code = main(
        [
            "explain-prompt",
            "Beautiful natural european scenery, 1920p or higher, 16:9, 20 individual clips",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["n_clips"] == 20
    assert payload["min_height"] == 1080
    assert payload["geo_requirement"] == "european"
    assert payload["allow_download"] is False


def test_run_requires_dry_run():
    with pytest.raises(SystemExit) as exc:
        main(["run", "--prompt", "alps"])
    assert exc.value.code == 2


def test_rank_requires_run_dir():
    with pytest.raises(SystemExit) as exc:
        main(["rank"])
    assert exc.value.code == 2


def test_analyze_requires_run_dir():
    with pytest.raises(SystemExit) as exc:
        main(["analyze"])
    assert exc.value.code == 2


def test_analyze_rejects_negative_or_nonfinite_limits(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text("[]")

    with pytest.raises(SystemExit) as negative:
        main(["analyze", "--run-dir", str(run_dir), "--max-videos", "-1"])
    assert negative.value.code == 2

    with pytest.raises(SystemExit) as nonfinite:
        main(["analyze", "--run-dir", str(run_dir), "--max-analysis-s", "nan"])
    assert nonfinite.value.code == 2


def test_analyze_returns_nonzero_for_partial_work(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text("[]")

    monkeypatch.setattr(
        "scenery_brief_clips.cli.analyze_run",
        lambda *args, **kwargs: [
            {"video_id": "abcdefghijk", "status": "partial", "errors": [{"error": "403"}], "excerpts": []}
        ],
    )
    code = main(
        [
            "analyze",
            "--run-dir",
            str(run_dir),
            "--root",
            str(tmp_path),
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["by_status"] == {"partial": 1}
    assert payload["range_errors"] == 1


def test_analyze_returns_structured_failure_when_generation_aborts(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text("[]")
    monkeypatch.setattr(
        "scenery_brief_clips.cli.analyze_run",
        lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("analysis inputs changed during analysis")
        ),
    )

    code = main(["analyze", "--run-dir", str(run_dir), "--root", str(tmp_path)])

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "failed"
    assert "inputs changed" in payload["error"]


def test_analyze_fails_closed_on_missing_status_or_recorded_errors(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text("[]")

    monkeypatch.setattr(
        "scenery_brief_clips.cli.analyze_run",
        lambda *args, **kwargs: [
            {
                "video_id": "abcdefghijk",
                "errors": [{"error": "hidden failure"}],
                "excerpts": [],
            }
        ],
    )
    code = main(
        [
            "analyze",
            "--run-dir",
            str(run_dir),
            "--root",
            str(tmp_path),
        ]
    )

    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["by_status"] == {"invalid": 1}
    assert payload["range_errors"] == 1


def test_apply_scores_exits_2_without_changing_ranked_on_invalid_scores(tmp_path, capsys):
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

    code = main(
        [
            "apply-scores",
            "--run-dir",
            str(run_dir),
            "--scores",
            str(scores),
            "--root",
            str(tmp_path),
        ]
    )

    assert code == 2
    assert json.loads(ranked_path.read_text()) == original
    assert not (run_dir / "ranked_before_vision.json").exists()
    assert "invalid vision scores" in capsys.readouterr().err


def _shortlist_fixture(tmp_path):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "constraint.json").write_text(json.dumps({"n_clips": 20}))
    ranked_path = run_dir / "ranked.json"
    ranked_path.write_text(json.dumps([{"video_id": "abcdefghijk", "priority": "promising"}]))
    cache_key = "abcdefghijk_0-8000_v3-video-only-720.mp4"
    excerpts_path = run_dir / "excerpts.json"
    excerpts_path.write_text(
        json.dumps(
            [
                {
                    "video_id": "abcdefghijk",
                    "title": "Colours",
                    "priority": "promising",
                    "status": "complete",
                    "copies": [
                        {
                            "path": str(run_dir / "media.mp4"),
                            "span": [0.0, 8.0],
                            "cache_key": cache_key,
                        }
                    ],
                    "excerpts": [
                        {
                            "start_s": 2.0,
                            "end_s": 8.0,
                            "source_scene": [2.0, 8.0],
                            "analysis_span": [0.0, 8.0],
                            "analysis_cache_key": cache_key,
                        }
                    ],
                    "errors": [],
                }
            ],
            indent=2,
        )
        + "\n"
    )
    excerpts_sha256 = sha256_file(excerpts_path)
    (run_dir / "analysis_manifest.json").write_text(
        json.dumps(
            {
                "schema_version": 3,
                "generation_id": "g" * 64,
                "ranked_sha256": sha256_file(ranked_path),
                "constraint_sha256": sha256_file(run_dir / "constraint.json"),
                "excerpts_sha256": excerpts_sha256,
                "settings": {},
            }
        )
    )
    (run_dir / "review.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "excerpts_sha256": excerpts_sha256,
                "generation_id": "g" * 64,
                "frames_per_moment": 2,
                "moments": [
                    {
                        "video_id": "abcdefghijk",
                        "excerpt_index": 0,
                        "start_s": 2.0,
                        "end_s": 8.0,
                        "analysis_cache_key": cache_key,
                        "frames": [],
                        "frame_hashes": ["0123456789abcdef"],
                    }
                ],
                "errors": [],
                "counts": {"moments": 1, "frames": 2, "errors": 0},
            }
        )
    )
    return run_dir


def _refresh_manifest_hashes(run_dir: Path) -> None:
    manifest_path = run_dir / "analysis_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["ranked_sha256"] = sha256_file(run_dir / "ranked.json")
    manifest["constraint_sha256"] = sha256_file(run_dir / "constraint.json")
    manifest["excerpts_sha256"] = sha256_file(run_dir / "excerpts.json")
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n", encoding="utf-8")
    review_path = run_dir / "review.json"
    if review_path.is_file():
        review = json.loads(review_path.read_text(encoding="utf-8"))
        if isinstance(review, dict) and "excerpts_sha256" in review:
            review["excerpts_sha256"] = manifest["excerpts_sha256"]
            review_path.write_text(json.dumps(review, indent=2) + "\n", encoding="utf-8")


@pytest.mark.parametrize(
    ("filename", "content", "refresh", "keyword"),
    [
        ("review.json", "[1, 2]", False, "review.json"),
        ("review.json", "null", False, "review.json"),
        ("analysis_manifest.json", "[]", False, "analysis_manifest.json"),
        ("analysis_manifest.json", "{ not json", False, "analysis_manifest.json"),
        ("constraint.json", "[]", True, "constraint.json"),
        ("constraint.json", "{ not json", True, "constraint.json"),
        ("ranked.json", "{}", True, "ranked.json"),
        ("excerpts.json", "{}", True, "excerpts.json"),
        ("excerpts.json", "{ not json", True, "excerpts.json"),
    ],
)
def test_shortlist_apply_reports_corrupt_run_files_cleanly(
    tmp_path, capsys, filename, content, refresh, keyword
):
    run_dir = _shortlist_fixture(tmp_path)
    (run_dir / filename).write_text(content, encoding="utf-8")
    if refresh:
        _refresh_manifest_hashes(run_dir)
    scores = run_dir / "shortlist_scores.json"
    scores.write_text(
        json.dumps(
            {
                "abcdefghijk": [
                    {"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "x"}
                ]
            }
        )
    )

    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )

    captured = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in captured.err
    assert keyword in captured.err
    assert not (run_dir / "shortlist.json").exists()


def test_shortlist_review_reports_corrupt_manifest_cleanly(tmp_path, capsys):
    run_dir = _shortlist_fixture(tmp_path)
    (run_dir / "analysis_manifest.json").write_text("{ not json", encoding="utf-8")

    code = main(["shortlist-review", "--run-dir", str(run_dir), "--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 1
    assert "Traceback" not in captured.err
    assert "analysis_manifest.json" in captured.err


def test_shortlist_apply_rejects_labels_outside_run_dir(tmp_path, capsys):
    run_dir = _shortlist_fixture(tmp_path)
    scores = tmp_path / "outside_scores.json"
    scores.write_text(
        json.dumps(
            {"abcdefghijk": [{"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "x"}]}
        )
    )

    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "must stay inside the run dir" in captured.err
    assert not (run_dir / "shortlist.json").exists()


def test_shortlist_apply_records_relative_labels_path(tmp_path, capsys):
    run_dir = _shortlist_fixture(tmp_path)
    scores = run_dir / "shortlist_scores.json"
    scores.write_text(
        json.dumps(
            {"abcdefghijk": [{"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "x"}]}
        )
    )

    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )

    assert code == 0
    capsys.readouterr()
    doc = json.loads((run_dir / "shortlist.json").read_text())
    assert doc["bindings"]["labels_path"] == "shortlist_scores.json"


@pytest.mark.parametrize(
    "command",
    ["verify", "shortlist-review", "shortlist-apply", "analyze", "rank", "apply-scores"],
)
def test_run_scoped_commands_reject_missing_run_dir_without_creating_it(
    tmp_path, capsys, command
):
    ghost = tmp_path / "ghost-run"
    args = [command, "--run-dir", str(ghost), "--root", str(tmp_path)]
    if command in ("shortlist-apply", "apply-scores"):
        args += ["--scores", str(tmp_path / "scores.json")]

    code = main(args)

    captured = capsys.readouterr()
    assert code == 2
    assert "Traceback" not in captured.err
    assert "run dir not found" in captured.err
    assert not ghost.exists()


def test_shortlist_apply_exits_2_on_invalid_labels_without_writing(tmp_path, capsys):
    run_dir = _shortlist_fixture(tmp_path)
    scores = run_dir / "shortlist_scores.json"
    scores.write_text(
        json.dumps({"abcdefghijk": [{"excerpt_index": 0, "match": "keeper", "geo": "supported", "scene_type": "x"}]})
    )

    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )

    assert code == 2
    assert not (run_dir / "shortlist.json").exists()
    assert "invalid shortlist labels" in capsys.readouterr().err


def test_shortlist_apply_exits_1_on_stale_review(tmp_path, capsys):
    run_dir = _shortlist_fixture(tmp_path)
    review = json.loads((run_dir / "review.json").read_text())
    review["excerpts_sha256"] = "f" * 64
    (run_dir / "review.json").write_text(json.dumps(review))
    scores = run_dir / "shortlist_scores.json"
    scores.write_text(
        json.dumps({"abcdefghijk": [{"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "x"}]})
    )

    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )

    assert code == 1
    assert "re-run shortlist-review" in capsys.readouterr().err


def test_shortlist_apply_writes_reproducible_shortlist(tmp_path, capsys):
    run_dir = _shortlist_fixture(tmp_path)
    scores = run_dir / "shortlist_scores.json"
    scores.write_text(
        json.dumps({"abcdefghijk": [{"excerpt_index": 0, "match": "keep", "geo": "uncertain", "scene_type": "mountains"}]})
    )

    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["selected"] == 1
    assert payload["shortfall"] == 19
    doc = json.loads((run_dir / "shortlist.json").read_text())
    assert doc["selected"][0]["flags"] == ["geo_uncertain"]
    first_bytes = (run_dir / "shortlist.json").read_bytes()

    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )
    assert code == 0
    capsys.readouterr()
    assert (run_dir / "shortlist.json").read_bytes() == first_bytes


def test_shortlist_apply_rejects_non_integer_n_clips(tmp_path, capsys):
    run_dir = _shortlist_fixture(tmp_path)
    constraint = json.loads((run_dir / "constraint.json").read_text())
    constraint["n_clips"] = 2.9
    (run_dir / "constraint.json").write_text(json.dumps(constraint))
    _refresh_manifest_hashes(run_dir)
    scores = run_dir / "shortlist_scores.json"
    scores.write_text(
        json.dumps(
            {"abcdefghijk": [{"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "x"}]}
        )
    )

    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )

    captured = capsys.readouterr()
    assert code == 2
    assert "n_clips" in captured.err
    assert not (run_dir / "shortlist.json").exists()


def test_shortlist_apply_enforces_labels_generation_binding(tmp_path, capsys):
    run_dir = _shortlist_fixture(tmp_path)
    excerpts_sha = sha256_file(run_dir / "excerpts.json")
    scores = run_dir / "shortlist_scores.json"
    labels_body = {"abcdefghijk": [{"excerpt_index": 0, "match": "keep", "geo": "supported", "scene_type": "x"}]}

    scores.write_text(json.dumps({"excerpts_sha256": excerpts_sha, **labels_body}))
    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )
    assert code == 0
    capsys.readouterr()

    scores.write_text(json.dumps({"excerpts_sha256": "f" * 64, **labels_body}))
    code = main(
        ["shortlist-apply", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "analysis generation" in captured.err


def test_apply_scores_reports_unmatched_ids(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(json.dumps([{"video_id": "abcdefghijk", "priority": "unknown"}]))
    scores = run_dir / "scores.json"
    scores.write_text(json.dumps({"zzzzzzzzzzz": [{"t_s": 1.0, "label": "keep"}]}))

    code = main(["apply-scores", "--run-dir", str(run_dir), "--scores", str(scores), "--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 0
    payload = json.loads(captured.out)
    assert payload["unmatched_score_ids"] == ["zzzzzzzzzzz"]
    assert "unmatched" in captured.err.lower()


def test_export_command_requires_authorization(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()

    code = main(["export", "--run-dir", str(run_dir), "--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 2
    assert "allow" in captured.err.lower()
    assert not (tmp_path / "out").exists()


def test_export_command_wires_authorization_and_exit_codes(tmp_path, monkeypatch, capsys):
    from scenery_brief_clips.export import ExportError, ExportStaleError

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seen = {}

    def fake_export(run_dir_arg, root_arg, theme=None, allow_export=False, config=None):
        seen.update({"allow": allow_export, "theme": theme, "config": config})
        return {"theme": theme or "t", "counts": {"failed": 0, "exported": 1}, "failed": []}

    monkeypatch.setattr("scenery_brief_clips.cli.export_run", fake_export)
    code = main(
        ["export", "--run-dir", str(run_dir), "--root", str(tmp_path), "--allow-export", "--theme", "my-theme"]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["counts"]["exported"] == 1
    assert seen["allow"] is True
    assert seen["theme"] == "my-theme"

    monkeypatch.setattr(
        "scenery_brief_clips.cli.export_run",
        lambda *a, **k: {"theme": "t", "counts": {"failed": 1, "exported": 0}, "failed": [{"reason": "x"}]},
    )
    assert main(["export", "--run-dir", str(run_dir), "--root", str(tmp_path), "--allow-export"]) == 1

    def raise_stale(*a, **k):
        raise ExportStaleError("stale")

    monkeypatch.setattr("scenery_brief_clips.cli.export_run", raise_stale)
    assert main(["export", "--run-dir", str(run_dir), "--root", str(tmp_path), "--allow-export"]) == 1

    def raise_error(*a, **k):
        raise ExportError("bad")

    monkeypatch.setattr("scenery_brief_clips.cli.export_run", raise_error)
    assert main(["export", "--run-dir", str(run_dir), "--root", str(tmp_path), "--allow-export"]) == 2


def test_export_command_reads_config_allow_export(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (tmp_path / "config.yaml").write_text("allow_export: true\n")
    seen = {}

    def fake_export(run_dir_arg, root_arg, theme=None, allow_export=False, config=None):
        seen.update({"allow": allow_export, "config": config})
        return {"theme": "t", "counts": {"failed": 0, "exported": 0}, "failed": []}

    monkeypatch.setattr("scenery_brief_clips.cli.export_run", fake_export)
    code = main(["export", "--run-dir", str(run_dir), "--root", str(tmp_path)])

    assert code == 0
    assert seen["allow"] is False
    assert seen["config"] == {"allow_export": True}


def test_verify_command_passes_require_export(tmp_path, monkeypatch, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    seen = {}

    def fake_verify(run_dir_arg, analysis_dir=None, probe_fn=None, decode_fn=None, root=None, require_export=False):
        seen.update({"root": root, "require_export": require_export})
        return {"ok": True, "errors": []}

    monkeypatch.setattr("scenery_brief_clips.cli.verify_run", fake_verify)
    code = main(["verify", "--run-dir", str(run_dir), "--root", str(tmp_path), "--require-export"])

    assert code == 0
    json.loads(capsys.readouterr().out)
    assert seen["require_export"] is True
    assert seen["root"] == tmp_path


def test_run_returns_nonzero_when_all_metadata_fetches_fail(tmp_path, monkeypatch, capsys):
    class BrokenMetaYt:
        def __init__(self, **kwargs):
            pass

        def search(self, query, limit):
            return [
                {"id": "id111111111", "title": "A"},
                {"id": "id222222222", "title": "B"},
            ]

        def fetch_metadata(self, video_id):
            raise RuntimeError("metadata down")

    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", BrokenMetaYt)
    code = main(
        [
            "run",
            "--dry-run",
            "--prompt",
            "alps 1080p 16:9 1 clips",
            "--root",
            str(tmp_path),
            "--sleep",
            "0",
        ]
    )
    payload = json.loads(capsys.readouterr().out)
    assert code == 1
    assert payload["stopped_reason"] == "metadata_errors"
    assert payload["candidates"] == 0


def test_analyze_rejects_invalid_duration_settings(tmp_path, capsys):
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / "ranked.json").write_text(
        json.dumps(
            [{"video_id": "abcdefghijk", "priority": "promising", "windows": [], "duration_s": 50.0}]
        )
    )
    (run_dir / "constraint.json").write_text(
        json.dumps({"target_duration_s": -5.0, "duration_min_s": 1.0, "duration_max_s": 12.0})
    )

    code = main(["analyze", "--run-dir", str(run_dir), "--root", str(tmp_path)])

    captured = capsys.readouterr()
    assert code == 2
    assert "duration settings" in captured.err
    assert not (run_dir / "excerpts.json").exists()


def test_run_returns_nonzero_on_search_failure(tmp_path, monkeypatch, capsys):
    class BrokenYt:
        def __init__(self, **kwargs):
            pass

        def search(self, query, limit):
            raise RuntimeError("search down")

    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", BrokenYt)
    code = main(
        [
            "run",
            "--dry-run",
            "--prompt",
            "alps 1080p 16:9 1 clips",
            "--root",
            str(tmp_path),
            "--sleep",
            "0",
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped_reason"] == "search_error"



def test_doctor_rejects_ytdlp_before_external_js_support(tmp_path, monkeypatch):
    from scenery_brief_clips import cli

    real_version = cli._version_line
    monkeypatch.setattr(
        cli,
        "_version_line",
        lambda cmd: "2024.08.06" if cmd[0].endswith("yt-dlp") else real_version(cmd),
    )
    report = doctor(tmp_path)
    assert report["ok"] is False
    assert any("yt-dlp 2024.08.06 is too old" in msg for msg in report["messages"])


def test_doctor_reports_versions(tmp_path):
    report = doctor(tmp_path)
    assert "versions" in report
    assert "python" in report["versions"]
    assert "yt_dlp" in report["versions"]
    assert "ffmpeg" in report["versions"]
    assert "ffprobe" in report["versions"]
    assert "node" in report["versions"]
    assert isinstance(report.get("messages"), list)
