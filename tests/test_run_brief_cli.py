import json
from pathlib import Path

import pytest

from scenery_brief_clips.brief import canonical_json_hash
from scenery_brief_clips.cli import main

from test_brief import valid_brief, valid_plan


class FakeYt:
    """Offline stand-in for YtDlp: records calls, never touches the network."""

    instances: list["FakeYt"] = []
    hits: list[tuple[str, str]] = []
    metadata: dict[str, dict] = {}

    def __init__(self, tmp_dir=None, allow_download=False, **kwargs):
        self.tmp_dir = tmp_dir
        self.allow_download = allow_download
        self.search_calls: list[tuple[str, int]] = []
        self.metadata_calls: list[str] = []
        FakeYt.instances.append(self)

    def search(self, query, limit):
        self.search_calls.append((query, limit))
        claimed = {vid for yt in FakeYt.instances for vid in yt.search_calls}
        seen_ids: set[str] = set()
        out = []
        for vid, title in FakeYt.hits:
            if vid in seen_ids:
                continue
            seen_ids.add(vid)
            out.append({"id": vid, "title": title})
            if len(out) >= limit:
                break
        return out

    def fetch_metadata(self, video_id):
        self.metadata_calls.append(video_id)
        return FakeYt.metadata[video_id]


def _wire_fake_yt(hits, metadata):
    FakeYt.hits = hits
    FakeYt.metadata = metadata
    FakeYt.instances = []


def _hd_info(video_id, title, duration=180):
    return {
        "id": video_id,
        "title": title,
        "duration": duration,
        "live_status": "not_live",
        "availability": "public",
        "formats": [
            {
                "format_id": "137",
                "width": 1920,
                "height": 1080,
                "fps": 30,
                "vcodec": "avc1",
                "acodec": "none",
            }
        ],
    }


def _write_brief_and_plan(tmp_path: Path):
    brief = valid_brief()
    plan = valid_plan(brief)
    brief_path = tmp_path / "brief.json"
    plan_path = tmp_path / "plan.json"
    brief_path.write_text(json.dumps(brief), encoding="utf-8")
    plan_path.write_text(json.dumps(plan), encoding="utf-8")
    return brief, plan, brief_path, plan_path


def test_run_brief_happy_path_writes_run_and_discovery(tmp_path, monkeypatch, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    _wire_fake_yt(
        [
            ("fixture_ok", "Alpaca field"),
            ("title_only", "Alpaca 4K UHD meadow"),
        ],
        {
            "fixture_ok": _hd_info("fixture_ok", "Alpaca field"),
            "title_only": {"id": "title_only", "title": "Alpaca 4K UHD meadow", "duration": 30, "formats": []},
        },
    )
    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", FakeYt)

    code = main(
        [
            "run-brief",
            "--brief", str(brief_path),
            "--plan", str(plan_path),
            "--dry-run",
            "--root", str(tmp_path),
            "--sleep", "0",
        ]
    )
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    run_dir = Path(payload["run_dir"])

    # Queries used verbatim, in plan order, without re-sort or dedupe.
    assert payload["queries"] == ["alpacas in a field", "alpaca pasture footage"]
    assert FakeYt.instances and FakeYt.instances[0].allow_download is False
    yt = FakeYt.instances[0]
    assert [call[0] for call in yt.search_calls] == ["alpacas in a field", "alpaca pasture footage"]
    assert payload["visual_status"] == "unverified"
    assert payload["acceptance_level"] == "metadata_only"

    candidates = json.loads((run_dir / "candidates.json").read_text(encoding="utf-8"))
    rejected = json.loads((run_dir / "rejected.json").read_text(encoding="utf-8"))
    assert [row["video_id"] for row in candidates] == ["fixture_ok"]
    assert [row["video_id"] for row in rejected] == ["title_only"]
    for row in candidates + rejected:
        assert row["visual_status"] == "unverified"
        assert row["acceptance_level"] == "metadata_only"

    discovery = json.loads((run_dir / "discovery.json").read_text(encoding="utf-8"))
    assert discovery["schema_version"] == "brief_discovery_v1"
    assert discovery["brief_sha256"] == canonical_json_hash(brief)
    assert discovery["query_plan"] == plan
    assert discovery["plan_provenance"]["instruction_version"] == "search_query_planner_v1"
    assert discovery["plan_provenance"]["plan_sha256"] == canonical_json_hash(plan)
    assert discovery["attempted_queries"] == ["alpacas in a field", "alpaca pasture footage"]
    assert discovery["counts"] == {"attempted_queries": 2, "distinct_ids": 2, "candidates": 1, "rejected": 1}
    assert discovery["stopped_reason"] == "complete"
    assert discovery["allow_download"] is False
    assert all(row["visual_status"] == "unverified" for row in discovery["candidates"])
    assert all(row["visual_status"] == "unverified" for row in discovery["rejected"])

    constraint = json.loads((run_dir / "constraint.json").read_text(encoding="utf-8"))
    assert constraint["theme_text"] == "alpacas in an outdoor field"
    assert constraint["n_clips"] == 3
    assert constraint["min_width"] == 1280
    assert constraint["min_height"] == 720
    assert constraint["geo_requirement"] == "none"
    assert constraint["allow_download"] is False
    assert constraint["limits"]["max_bytes"] == 0
    assert constraint["limits"]["max_seconds"] == 120
    assert constraint["limits"]["max_search_results"] == 20


def test_run_brief_title_only_4k_cannot_pass_format_gate(tmp_path, monkeypatch, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    brief["source_geometry"]["min_width"] = 3840
    brief["source_geometry"]["min_height"] = 2160
    brief["sources"]["source_geometry"]["origin"] = "user_clarification"
    brief["sources"]["source_geometry"]["quote"] = "4K means 3840x2160"
    plan["brief_sha256"] = canonical_json_hash(brief)
    (tmp_path / "brief.json").write_text(json.dumps(brief), encoding="utf-8")
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    _wire_fake_yt(
        [("title_4k", "Alpaca 4K UHD meadow compilation")],
        {"title_4k": {"id": "title_4k", "title": "Alpaca 4K UHD meadow", "duration": 30, "formats": []}},
    )
    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", FakeYt)

    code = main(
        [
            "run-brief",
            "--brief", str(tmp_path / "brief.json"),
            "--plan", str(tmp_path / "plan.json"),
            "--dry-run",
            "--root", str(tmp_path),
            "--sleep", "0",
        ]
    )
    assert code == 0
    run_dir = Path(json.loads(capsys.readouterr().out)["run_dir"])
    candidates = json.loads((run_dir / "candidates.json").read_text(encoding="utf-8"))
    rejected = json.loads((run_dir / "rejected.json").read_text(encoding="utf-8"))
    assert candidates == []
    assert [row["video_id"] for row in rejected] == ["title_4k"]
    assert rejected[0]["reason"] == "no_min_resolution"


def test_run_brief_max_results_flag_only_tightens(tmp_path, monkeypatch, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    _wire_fake_yt(
        [("id111111111", "A"), ("id222222222", "B"), ("id333333333", "C")],
        {vid: _hd_info(vid, vid) for vid in ("id111111111", "id222222222", "id333333333")},
    )
    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", FakeYt)

    code = main(
        [
            "run-brief",
            "--brief", str(brief_path),
            "--plan", str(plan_path),
            "--dry-run",
            "--root", str(tmp_path),
            "--sleep", "0",
            "--max-results", "2",
        ]
    )
    assert code == 0
    run_dir = Path(json.loads(capsys.readouterr().out)["run_dir"])
    constraint = json.loads((run_dir / "constraint.json").read_text(encoding="utf-8"))
    assert constraint["limits"]["max_search_results"] == 2
    assert constraint["limits"]["max_metadata_fetches"] == 30


def test_run_brief_flag_cannot_loosen_brief_limits(tmp_path, monkeypatch, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    brief["search_limits"]["max_search_results"] = 5
    (tmp_path / "brief.json").write_text(json.dumps(brief), encoding="utf-8")
    plan["brief_sha256"] = canonical_json_hash(brief)
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    _wire_fake_yt([], {})
    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", FakeYt)

    code = main(
        [
            "run-brief",
            "--brief", str(tmp_path / "brief.json"),
            "--plan", str(tmp_path / "plan.json"),
            "--dry-run",
            "--root", str(tmp_path),
            "--sleep", "0",
            "--max-results", "20",
        ]
    )
    assert code == 0
    run_dir = Path(json.loads(capsys.readouterr().out)["run_dir"])
    constraint = json.loads((run_dir / "constraint.json").read_text(encoding="utf-8"))
    assert constraint["limits"]["max_search_results"] == 5


def test_run_brief_empty_plan_stops_without_ytdlp(tmp_path, monkeypatch, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    plan["queries"] = []
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    called = []

    class BoomYt:
        def __init__(self, **kwargs):
            called.append("init")

        def search(self, query, limit):
            called.append("search")
            raise AssertionError("yt-dlp must not be called for an empty plan")

    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", BoomYt)

    code = main(
        [
            "run-brief",
            "--brief", str(brief_path),
            "--plan", str(plan_path),
            "--dry-run",
            "--root", str(tmp_path),
        ]
    )
    assert code == 1
    payload = json.loads(capsys.readouterr().out)
    assert payload["stopped_reason"] == "empty_query_plan"
    assert called == []


def test_run_brief_invalid_brief_exits_2(tmp_path, capsys):
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps({"schema_version": "search_brief_v1"}), encoding="utf-8")
    code = main(["run-brief", "--brief", str(brief_path), "--dry-run", "--root", str(tmp_path)])
    captured = capsys.readouterr()
    assert code == 2
    assert "invalid brief" in captured.err
    assert "clarification_required" in captured.err


def test_run_brief_missing_brief_file_exits_2(tmp_path, capsys):
    code = main(
        ["run-brief", "--brief", str(tmp_path / "nope.json"), "--dry-run", "--root", str(tmp_path)]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "brief file not found" in captured.err


def test_run_brief_without_brief_flag_exits_2():
    with pytest.raises(SystemExit) as exc:
        main(["run-brief", "--dry-run"])
    assert exc.value.code == 2


def test_run_brief_without_dry_run_flag_exits_2(tmp_path):
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(valid_brief()), encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        main(["run-brief", "--brief", str(brief_path)])
    assert exc.value.code == 2


def test_run_brief_plan_hash_mismatch_exits_2(tmp_path, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    plan["brief_sha256"] = "2" * 64
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")
    code = main(
        [
            "run-brief",
            "--brief", str(brief_path),
            "--plan", str(plan_path),
            "--dry-run",
            "--root", str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "brief_hash_mismatch" in captured.err


def test_run_brief_uses_planner_model_when_no_plan(tmp_path, monkeypatch, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    (tmp_path / "planner.yaml").write_text("backend: codex-login\nmodel: gpt-test\n", encoding="utf-8")
    _wire_fake_yt(
        [("fixture_ok", "Alpaca field")],
        {"fixture_ok": _hd_info("fixture_ok", "Alpaca field")},
    )
    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", FakeYt)

    planner_calls = []

    def fake_plan_queries(brief_arg, wire, caller=None):
        planner_calls.append((brief_arg, wire))
        provenance = {
            "schema_version": "brief_plan_provenance_v1",
            "instruction_version": "search_query_planner_v1",
            "backend": "codex-login",
            "model": "gpt-test",
            "elapsed_s": 0.1,
            "plan_sha256": canonical_json_hash(plan),
        }
        return plan, provenance

    monkeypatch.setattr("scenery_brief_clips.cli.plan_queries", fake_plan_queries)

    code = main(
        [
            "run-brief",
            "--brief", str(brief_path),
            "--dry-run",
            "--root", str(tmp_path),
            "--sleep", "0",
        ]
    )
    assert code == 0
    out = capsys.readouterr().out
    # First JSON blob printed is the plan provenance; the summary follows.
    decoder = json.JSONDecoder()
    payloads = []
    index = 0
    while index < len(out):
        stripped = out[index:].lstrip()
        if not stripped:
            break
        payload, consumed = decoder.raw_decode(stripped)
        payloads.append(payload)
        index += len(out) - index - len(stripped) + consumed
    assert payloads[0]["plan_provenance"]["model"] == "gpt-test"
    assert payloads[1]["stopped_reason"] == "complete"
    run_dir = Path(payloads[1]["run_dir"])
    discovery = json.loads((run_dir / "discovery.json").read_text(encoding="utf-8"))
    assert discovery["query_plan"] == plan
    assert discovery["plan_provenance"]["backend"] == "codex-login"
    assert planner_calls and planner_calls[0][0] == brief


def test_run_brief_missing_planner_yaml_exits_2(tmp_path, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    code = main(
        [
            "run-brief",
            "--brief", str(brief_path),
            "--dry-run",
            "--root", str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "planner.yaml is missing" in captured.err


def test_run_brief_planner_model_failure_exits_2(tmp_path, monkeypatch, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    planner_dir = tmp_path / "planner"
    planner_dir.mkdir()
    (planner_dir / "planner.yaml").write_text("backend: codex-login\nmodel: gpt-test\n", encoding="utf-8")

    def failing_plan_queries(brief_arg, wire, caller=None):
        from scenery_brief_clips.planner import PlannerError

        raise PlannerError("planner model returned garbage")

    monkeypatch.setattr("scenery_brief_clips.cli.plan_queries", failing_plan_queries)

    code = main(
        [
            "run-brief",
            "--brief", str(brief_path),
            "--dry-run",
            "--root", str(tmp_path),
            "--planner-config", str(planner_dir / "planner.yaml"),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "planner failed" in captured.err


def test_run_brief_planner_config_override(tmp_path, monkeypatch, capsys):
    brief, plan, brief_path, plan_path = _write_brief_and_plan(tmp_path)
    custom = tmp_path / "custom-planner.yaml"
    custom.write_text("backend: codex-login\nmodel: custom-model\n", encoding="utf-8")
    seen_wires = []

    def fake_plan_queries(brief_arg, wire, caller=None):
        seen_wires.append(wire)
        provenance = {
            "schema_version": "brief_plan_provenance_v1",
            "instruction_version": "search_query_planner_v1",
            "backend": wire.backend,
            "model": wire.model,
            "elapsed_s": 0.1,
            "plan_sha256": canonical_json_hash(plan),
        }
        return plan, provenance

    monkeypatch.setattr("scenery_brief_clips.cli.plan_queries", fake_plan_queries)
    _wire_fake_yt([], {})
    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", FakeYt)

    code = main(
        [
            "run-brief",
            "--brief", str(brief_path),
            "--dry-run",
            "--root", str(tmp_path),
            "--planner-config", str(custom),
        ]
    )
    assert code == 0
    assert seen_wires[0].model == "custom-model"


def test_run_brief_missing_plan_file_exits_2(tmp_path, capsys):
    brief_path = tmp_path / "brief.json"
    brief_path.write_text(json.dumps(valid_brief()), encoding="utf-8")
    code = main(
        [
            "run-brief",
            "--brief", str(brief_path),
            "--plan", str(tmp_path / "nope.json"),
            "--dry-run",
            "--root", str(tmp_path),
        ]
    )
    captured = capsys.readouterr()
    assert code == 2
    assert "plan file not found" in captured.err


def test_legacy_run_dry_run_still_works(tmp_path, monkeypatch, capsys):
    class FakeLegacyYt:
        def __init__(self, **kwargs):
            self.search_calls = []

        def search(self, query, limit):
            self.search_calls.append((query, limit))
            return [{"id": "id111111111", "title": "Fjord"}]

        def fetch_metadata(self, video_id):
            return _hd_info("id111111111", "Fjord")

    monkeypatch.setattr("scenery_brief_clips.cli.YtDlp", FakeLegacyYt)
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
    assert code == 0
    payload = json.loads(capsys.readouterr().out)
    run_dir = Path(payload["run_dir"])
    assert (run_dir / "candidates.json").is_file()
    assert (run_dir / "rejected.json").is_file()
    assert (run_dir / "log.txt").is_file()
    # The legacy path keeps its own query building and no discovery sidecar.
    assert not (run_dir / "discovery.json").exists()
    candidates = json.loads((run_dir / "candidates.json").read_text(encoding="utf-8"))
    assert candidates and candidates[0]["video_id"] == "id111111111"
    assert "visual_status" not in candidates[0]


def test_run_dry_with_explicit_queries_uses_them_verbatim(tmp_path, monkeypatch):
    from scenery_brief_clips.models import Constraint
    from scenery_brief_clips.pipeline import run_dry
    from scenery_brief_clips.store import MetadataCache

    class ProbeYt:
        def __init__(self, **kwargs):
            self.search_calls = []

        def search(self, query, limit):
            self.search_calls.append(query)
            return []

        def fetch_metadata(self, video_id):
            raise AssertionError("no ids to fetch")

    yt = ProbeYt()
    constraint = Constraint(theme_text="alps")
    cache = MetadataCache(tmp_path / "cache")
    provided = ["zebra crossing", "alpine meadow", "zebra crossing"]
    result = run_dry(constraint, yt=yt, cache=cache, sleep_fn=lambda _s: None, queries=provided)
    # Verbatim: order preserved, duplicates not removed.
    assert result.queries == provided
    assert yt.search_calls == provided


def test_run_dry_without_queries_still_builds_legacy_queries(tmp_path):
    from scenery_brief_clips.models import Constraint
    from scenery_brief_clips.pipeline import run_dry
    from scenery_brief_clips.store import MetadataCache

    class ProbeYt:
        def __init__(self, **kwargs):
            self.search_calls = []

        def search(self, query, limit):
            self.search_calls.append(query)
            return []

        def fetch_metadata(self, video_id):
            raise AssertionError("no ids to fetch")

    yt = ProbeYt()
    constraint = Constraint(theme_text="alps")
    result = run_dry(constraint, yt=yt, cache=MetadataCache(tmp_path / "cache"), sleep_fn=lambda _s: None)
    assert result.queries == ["alps", "alps 4k", "alps compilation", "alps drone"]


def test_new_modules_are_stdlib_only():
    import ast
    import pathlib
    import sys

    # The new Part 7 modules must stay plain-stdlib; pre-existing modules
    # (cli.py imports PIL in doctor) keep their existing dependency set.
    src_dir = pathlib.Path(__file__).resolve().parents[1] / "src" / "scenery_brief_clips"
    allowed_local = {"scenery_brief_clips"}
    for name in ("brief.py", "planner.py", "pipeline.py"):
        tree = ast.parse((src_dir / name).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    root = alias.name.split(".")[0]
                    assert root in allowed_local or root in sys.stdlib_module_names, (name, alias.name)
            elif isinstance(node, ast.ImportFrom):
                root = (node.module or "").split(".")[0]
                assert root in allowed_local or root in sys.stdlib_module_names, (name, node.module)
